"""Semantic SQL runtime: api.recall composed with SQL, placement rules, taint
analysis, budgets, the cost gate, episode grain (migration 0040).

Synthetic data only; every operator is local and deterministic.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message

from throughline import access, cost, resultsets, semantic, semantic_ops
from throughline.db import connect, create_token
from throughline.providers.base import Estimate
from throughline.query import FUNCTIONS, query
from throughline.scope import Scope
from throughline.semantic_ops import SemanticOp

from test_security import ESCAPES

ZERO = "rs_" + "0" * 32
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
AGGREGATE = semantic.AGGREGATE_WARNING
GRAIN = semantic.GRAIN_CODE


# --- helpers -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def client_token():
    return owner(
        "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id DESC LIMIT 1"
    )[0]


def client_scope(asker=None):
    return Scope.from_token(dict(client_token(), asker=asker))


def sql(client, statement, headers=None, **kwargs):
    return client.post(
        "/v1/sql", json=dict(statement=statement, **kwargs), headers=headers or {}
    )


def seed(client, n=5, channel="synthetic.chat", start=0, **kwargs):
    if channel != "synthetic.chat":
        kwargs.setdefault("fields", {})
    messages = [
        message(i, channel=channel, external_id=f"{channel}-{i}", **kwargs)
        for i in range(start, start + n)
    ]
    r = ingest(client, messages)
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text
    rows = owner(
        "SELECT m.id,m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL",
        (channel,),
    )
    found = {r["external_id"]: r["id"] for r in rows}
    return [found[f"{channel}-{i}"] for i in range(start, start + n)]


def add_channel(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text


class Calls:
    def __init__(self):
        self.runs = []
        self.lock = threading.Lock()

    def add(self, item):
        with self.lock:
            self.runs.append(item)


@pytest.fixture()
def recall_calls(monkeypatch):
    """Counts calls of the recall library made by api.recall."""
    module = __import__("throughline.semantic_ops.recall", fromlist=["x"])
    calls = Calls()
    original = module.recall_library

    def counted(*args, **kwargs):
        calls.add(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "recall_library", counted)
    return calls


def passthrough(name="fakepass", cost_each=0.0, local=True, sleep_s=0.0, calls=None):
    """A test operator returning its input set (or an empty exhaustive set)."""
    calls = calls if calls is not None else Calls()

    def estimate(ctx, args):
        return [
            Estimate(
                purpose="fake",
                provider="fake",
                model="fake-model",
                calls=1,
                tokens=10,
                cost=cost_each,
                local=local,
            )
        ]

    def run(ctx, args):
        calls.add(dict(args=dict(args), tracker=ctx.tracker, budget=ctx.tracker.budget))
        if sleep_s:
            time.sleep(sleep_s)
        if args.get("input"):
            return args["input"]
        return resultsets.create(ctx.scope, [], completeness="exhaustive")

    op = SemanticOp(
        name=name,
        params={"input": "handle", "note": "text", "n": "int"},
        input_params=("input",),
        exact=True,
        cost_class="free" if local else "model",
        estimate=estimate,
        run=run,
    )
    return op, calls


@pytest.fixture()
def fake_op():
    op, calls = passthrough()
    semantic_ops.register(op)
    yield calls
    semantic_ops.unregister(op.name)


def ledger_rows():
    return owner("SELECT count(*) n FROM tl.model_ledger")[0]["n"]


# --- QRY-31 ------------------------------------------------------------------


def test_qry_31_recall_composes_with_sql(client, channel, recall_calls):
    seed(client, 6)
    r = sql(
        client,
        "SELECT r.record_id, r.ordinal, m.channel, m.content FROM api.recall(query => 'orchard', k => 3) r"
        " JOIN api.messages m ON m.id = r.message_id ORDER BY r.ordinal",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 3
    assert {row["channel"] for row in body["rows"]} == {"synthetic.chat"}
    assert all(row["record_id"].startswith("m:") for row in body["rows"])
    execution = body["execution"]
    (call,) = execution["semantic_calls"]
    assert call["name"] == "recall" and call["alias"] == "r"
    assert call["completeness"] == "ranked_sample" and call["member_count"] == 3
    assert semantic.HANDLE.fullmatch(call["handle"])
    assert len(recall_calls.runs) == 1
    # The handle is one of the caller's sets: readable in later statements.
    again = sql(client, f"SELECT count(*) AS n FROM api.result_set('{call['handle']}')")
    assert again.json()["rows"][0]["n"] == 3
    # The CTE form composes too, and a ranked recall is a sample.
    r = sql(
        client,
        "WITH hits AS (SELECT * FROM api.recall('orchard', 2)) SELECT count(*) AS n FROM hits",
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"n": 2}]
    assert AGGREGATE in r.json()["execution"]["warnings"]


def test_qry_31_validated_before_any_work(client, channel, recall_calls):
    seed(client, 2)
    for statement in (
        "SELECT * FROM api.recall(query => 'orchard') r JOIN tl.messages t ON true",
        "SELECT pg_sleep(1) FROM api.recall(query => 'orchard') r",
        "SELECT * FROM api.recall(query => 'orchard') r FOR UPDATE",
    ):
        r = sql(client, statement)
        assert r.status_code == 400, (statement, r.text)
    assert recall_calls.runs == []
    assert owner("SELECT count(*) n FROM tl.result_sets")[0]["n"] == 0
    assert ledger_rows() == 0


# --- QRY-37 ------------------------------------------------------------------


REJECTIONS = [
    (
        "SELECT * FROM api.messages m WHERE api.recall('orchard') IS NULL",
        "WHERE",
    ),
    ("SELECT api.recall('orchard')", "target list"),
    (
        "SELECT * FROM api.messages m, LATERAL api.recall(m.content) r",
        "LATERAL",
    ),
    (
        "WITH RECURSIVE c AS (SELECT * FROM api.recall('orchard')) SELECT * FROM c",
        "recursive CTE",
    ),
    ("SELECT * FROM api.recall('orchard') WITH ORDINALITY r", "WITH ORDINALITY"),
    (
        "SELECT * FROM api.messages m, api.recall(m.content) r",
        "column-reference argument",
    ),
    (
        "SELECT * FROM api.recall(query => (SELECT 'orchard')) r",
        "subquery argument",
    ),
    ("SELECT * FROM api.recall(nope => 'orchard') r", "unknown parameter nope"),
    (
        "SELECT * FROM api.recall('a') a, api.recall('b') b, api.recall('c') c,"
        " api.recall('d') d, api.recall('e') e",
        "more than 4 semantic calls",
    ),
    (
        "SELECT * FROM api.fakepass(input_sql => 'SELECT id FROM api.messages') f",
        "input_sql without max_rows",
    ),
    (
        "SELECT * FROM api.fakepass(input_sql => 'SELECT * FROM api.recall(''x'')', max_rows => 5) f",
        "nested semantic call",
    ),
    (
        "SELECT * FROM api.fakepass(input_sql => 'SELECT * FROM tl.messages', max_rows => 5) f",
        "invalid input_sql",
    ),
    ("SELECT * FROM api.recall('orchard') r(x int)", "column definition list"),
    ("SELECT * FROM api.recall(k => 3) r", "missing parameter query"),
    (
        "SELECT * FROM api.fakepass(input => 'not-a-handle') f",
        "must be a result set handle",
    ),
    (
        "SELECT * FROM api.messages m JOIN api.messages n ON api.recall('x') IS NULL",
        "JOIN ON",
    ),
    ("SELECT count(*) FROM api.messages GROUP BY api.recall('x')", "GROUP BY"),
    ("SELECT count(*) FROM api.messages HAVING api.recall('x') IS NULL", "HAVING"),
    ("SELECT * FROM api.messages ORDER BY api.recall('x')", "ORDER BY"),
    ("SELECT count(api.recall('x')) FROM api.messages", "aggregate argument"),
    (
        "SELECT row_number() OVER (PARTITION BY api.recall('x')) FROM api.messages",
        "window",
    ),
    (
        "SELECT * FROM ROWS FROM (api.recall('x'), api.recall('y')) r",
        "ROWS FROM with several functions",
    ),
    ("SELECT * FROM api.recall($1) r", "parameter argument"),
    ("SELECT * FROM api.recall('x' || 'y') r", "non-constant argument"),
    ("SELECT * FROM api.recall(query => 'x', query => 'y') r", "given twice"),
]


@pytest.mark.parametrize("statement,construct", REJECTIONS)
def test_qry_37_rejections(
    client, channel, recall_calls, fake_op, statement, construct
):
    r = sql(client, statement)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "is not supported here" in detail and construct in detail, detail
    assert recall_calls.runs == [] and fake_op.runs == []
    assert owner("SELECT count(*) n FROM tl.result_sets")[0]["n"] == 0
    assert ledger_rows() == 0


def test_qry_37_input_sql_bounded(client, channel, fake_op):
    seed(client, 5)
    r = sql(
        client,
        "SELECT f.record_id FROM api.fakepass(input_sql => 'SELECT id FROM api.messages ORDER BY id', max_rows => 3) f",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 3
    (call,) = body["execution"]["semantic_calls"]
    assert call["completeness"] == "truncated"
    r = sql(
        client,
        "SELECT f.record_id FROM api.fakepass(input_sql => 'SELECT id FROM api.messages', max_rows => 10) f",
    )
    assert r.json()["execution"]["semantic_calls"][0]["completeness"] == "exhaustive"
    for bad in (0, 10001):
        r = sql(
            client,
            f"SELECT * FROM api.fakepass(input_sql => 'SELECT id FROM api.messages', max_rows => {bad}) f",
        )
        assert r.status_code == 400 and "max_rows" in r.json()["detail"]
    r = sql(
        client,
        "SELECT * FROM api.fakepass(input_sql => 'SELECT content FROM api.messages', max_rows => 5) f",
    )
    assert r.status_code == 400 and "id or message_id" in r.json()["detail"]
    assert len(fake_op.runs) == 2


# --- QRY-38 ------------------------------------------------------------------


def test_qry_38_taint(client, channel):
    ids = seed(client, 4)
    scope = client_scope()
    ranked = resultsets.create(
        scope, [dict(record_id=f"m:{i}") for i in ids], completeness="ranked_sample"
    )
    exhaustive = resultsets.create(
        scope, [dict(record_id=f"m:{i}") for i in ids], completeness="exhaustive"
    )
    tainted = [
        "SELECT count(*) FROM api.search('orchard', 20) s",
        "SELECT count(*) FROM api.search('orchard') s",
        "WITH h AS (SELECT * FROM api.search('orchard', 20)) SELECT count(*) FROM h",
        "SELECT count(*) FROM (SELECT * FROM api.search('orchard', 20)) h",
        "SELECT count(*) FROM api.search_records('orchard', 20) s",
        "SELECT count(*) FROM api.search_passages('orchard', 20) s",
        "SELECT count(*) FROM api.entity_search('orchard', 20) s",
        "SELECT count(*) OVER () FROM api.search('orchard', 20) s",
        "SELECT DISTINCT s.score FROM api.search('orchard', 20) s",
        "SELECT s.score FROM api.search_filtered('orchard', 20) s GROUP BY s.score",
        f"SELECT count(*) FROM api.result_set('{ranked}') r",
        "SELECT count(*) FROM (SELECT * FROM api.messages LIMIT 2) m",
        "SELECT count(*) FROM api.messages m WHERE m.id IN (SELECT id FROM api.search_filtered('orchard', 20))",
        "SELECT count(*) FROM api.messages m JOIN api.messages n"
        " ON n.id = m.id AND n.id IN (SELECT id FROM api.search_filtered('orchard', 20))",
    ]
    for statement in tainted:
        r = sql(client, statement)
        assert r.status_code == 200, (statement, r.text)
        assert AGGREGATE in r.json()["execution"]["warnings"], statement
        r = sql(client, statement, strict_exact=True)
        assert r.status_code == 400, (statement, r.text)
        assert AGGREGATE in r.json()["detail"]
    clean = [
        "SELECT count(*) FROM api.messages",
        "SELECT count(*) FROM api.search('orchard', NULL) s",
        "SELECT count(*) FROM api.search_filtered('orchard', NULL::int) s",
        f"SELECT count(*) FROM api.result_set('{exhaustive}') r",
        "SELECT s.score FROM api.search('orchard', 20) s",
    ]
    for statement in clean:
        r = sql(client, statement, strict_exact=True)
        assert r.status_code == 200, (statement, r.text)
        assert AGGREGATE not in r.json()["execution"]["warnings"], statement
    # Every statement that reads a handle reports its completeness.
    r = sql(client, f"SELECT count(*) FROM api.result_set('{ranked}') r")
    assert r.json()["execution"]["handles"] == [
        dict(handle=ranked, completeness="ranked_sample", member_count=4)
    ]
    # The analysis names the tainted relations.
    names = semantic.taint(
        semantic._tree(
            "WITH h AS (SELECT * FROM api.search('x', 5)) SELECT * FROM h JOIN "
            f"api.result_set('{ranked}') r ON true JOIN api.messages m ON true"
        ),
        {ranked: "ranked_sample"},
    )
    assert {"h", "r"} <= set(names) and "m" not in names
    # A sampled read is not a population either (api relations are views,
    # which Postgres cannot sample today; the rule holds if that changes).
    sampled = semantic.analyze(
        "SELECT count(*) FROM api.messages m TABLESAMPLE BERNOULLI (50)",
        handles={},
        ranked={},
    )
    assert sampled.findings and sampled.taints == {"m": "TABLESAMPLE"}


def test_qry_38_input_sql_completeness(client, channel, fake_op):
    seed(client, 5)
    r = sql(
        client,
        "SELECT * FROM api.fakepass(input_sql => 'SELECT id FROM api.search_filtered(''orchard'', 200)', max_rows => 100) f",
    )
    assert r.status_code == 200, r.text
    execution = r.json()["execution"]
    assert execution["semantic_calls"][0]["completeness"] == "ranked_sample"
    stage = [s for s in execution["stages"] if s["name"] == "input_sql"][0]
    assert stage["completeness"] == "ranked_sample" and stage["rows"] == 5
    r = sql(
        client,
        "SELECT count(*) FROM api.fakepass(input_sql => 'SELECT id FROM api.search_filtered(''orchard'', NULL)', max_rows => 100) f",
        strict_exact=True,
    )
    assert r.status_code == 200, r.text
    assert r.json()["execution"]["semantic_calls"][0]["completeness"] == "exhaustive"


# --- QRY-36, CST-4 -------------------------------------------------------------


def test_qry_36_execution_metadata_and_trace(client, channel):
    seed(client, 4)
    r = sql(
        client,
        "SELECT r.record_id FROM api.recall(query => 'orchard', k => 2) r",
        trace=True,
    )
    assert r.status_code == 200, r.text
    execution = r.json()["execution"]
    for key in (
        "status",
        "truncation",
        "completeness",
        "stages",
        "timings_ms",
        "incomplete",
        "budget",
        "warnings",
        "trace_id",
        "plan",
        "estimate",
        "semantic_calls",
    ):
        assert key in execution, key
    names = [s["name"] for s in execution["stages"]]
    assert "semantic:recall" in names and "sql" in names
    assert execution["plan"][0]["name"] == "recall"
    assert execution["budget"]["limits"]["deadline_ms"] is not None
    trace_id = execution["trace_id"]
    events = owner(
        "SELECT op,statement,detail FROM tl.trace_events WHERE trace_id=%s ORDER BY seq",
        (trace_id,),
    )
    ops = [e["op"] for e in events]
    assert "semantic.recall" in ops and "sql" in ops
    assert "Synthetic orchard message" not in str(events)
    audit = owner(
        "SELECT detail FROM tl.audit_log WHERE detail->>'trace_id'=%s", (trace_id,)
    )
    assert audit


def test_qry_36_trace_written_with_timestamp_arguments(client, channel):
    """Timestamp arguments (and coverage times) in the trace are stored as
    ISO text; the trace is written, never dropped with a warning."""
    seed(client, 3)
    r = sql(
        client,
        "SELECT r.record_id FROM api.recall(query => 'orchard', after => '2019-12-31Z', before => '2020-02-01Z') r",
        trace=True,
    )
    assert r.status_code == 200, r.text
    execution = r.json()["execution"]
    assert "trace was not written" not in execution["warnings"]
    assert execution["trace_id"]
    stored = owner(
        "SELECT execution FROM tl.traces WHERE id=%s", (execution["trace_id"],)
    )
    assert stored and stored[0]["execution"] is not None


def test_cst_4_shared_budget_and_deadline(client, channel):
    seen = Calls()
    a, _ = passthrough("fakea", calls=seen)
    b, _ = passthrough("fakeb", calls=seen)
    slow, _ = passthrough("fakeslow", sleep_s=2.0, calls=seen)
    for op in (a, b, slow):
        semantic_ops.register(op)
    try:
        r = sql(
            client,
            "SELECT * FROM api.fakea() x, api.fakeb() y",
            budgets=dict(model_cost=0.05, deadline_ms=20000),
        )
        assert r.status_code == 200, r.text
        trackers = {id(run["tracker"]) for run in seen.runs}
        assert len(seen.runs) == 2 and len(trackers) == 1
        budget = seen.runs[0]["budget"]
        assert budget.model_cost == 0.05 and budget.deadline_ms == 20000
        # The deadline covers the operators; a call past it fails the statement.
        started = time.perf_counter()
        r = sql(client, "SELECT * FROM api.fakeslow() s", budgets=dict(deadline_ms=300))
        assert r.status_code == 400 and "deadline" in r.json()["detail"], r.text
        assert time.perf_counter() - started < 1.9
        # A caller budget never raises the token's limit.
        r = sql(client, "SELECT * FROM api.fakea() x", budgets=dict(model_cost=99))
        assert seen.runs[-1]["budget"].model_cost == cost.DEFAULT_MODEL_COST
    finally:
        for op in (a, b, slow):
            semantic_ops.unregister(op.name)


# --- CST-10 ------------------------------------------------------------------


def _approve(approval_id):
    with connect("app") as c:
        cost.decide(c, approval_id, "approved", by="synthetic owner")


def test_cst_10_approval_required_zero_calls_then_once(client, channel):
    seed(client, 3)
    op, calls = passthrough("fakecostly", cost_each=1.0, local=False)
    semantic_ops.register(op)
    statement = "SELECT f.record_id FROM api.fakecostly(input_sql => 'SELECT id FROM api.messages', max_rows => 50) f"
    try:
        r = sql(client, statement)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"] == [] and body["row_count"] == 0
        execution = body["execution"]
        assert execution["status"] == "approval_required"
        approval = execution["approval"]["approval_id"]
        assert calls.runs == [] and ledger_rows() == 0
        _approve(approval)
        r = sql(client, statement, approval_id=approval)
        assert r.json()["execution"]["status"] == "complete", r.text
        assert r.json()["row_count"] == 3 and len(calls.runs) == 1
        # Used once: the same approval does not run it again.
        r = sql(client, statement, approval_id=approval)
        assert r.json()["execution"]["status"] == "approval_required"
        assert len(calls.runs) == 1

        def fresh():
            response = sql(client, statement)
            pending = response.json()["execution"]["approval"]["approval_id"]
            _approve(pending)
            return pending

        # Not reusable under another asker ...
        approval = fresh()
        r = sql(
            client,
            statement,
            approval_id=approval,
            headers={"X-Throughline-Asker": "fake-other-asker"},
        )
        assert r.json()["execution"]["status"] == "approval_required"
        # ... with other budgets ...
        r = sql(client, statement, approval_id=approval, budgets=dict(tokens=1234))
        assert r.json()["execution"]["status"] == "approval_required"
        # ... or once the input has another member count.
        seed(client, 1, start=10)
        r = sql(client, statement, approval_id=approval)
        assert r.json()["execution"]["status"] == "approval_required"
        assert len(calls.runs) == 1
    finally:
        semantic_ops.unregister(op.name)


def test_dry_run(client, channel, recall_calls):
    seed(client, 3)
    r = sql(
        client,
        "SELECT * FROM api.recall(query => 'orchard', k => 2) r",
        dry_run=True,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == [] and body["row_count"] == 0
    execution = body["execution"]
    assert execution["dry_run"] is True and execution["status"] == "complete"
    assert [p["name"] for p in execution["plan"]] == ["recall"]
    assert execution["estimate"]["cost"] == 0
    assert recall_calls.runs == []
    assert owner("SELECT count(*) n FROM tl.result_sets")[0]["n"] == 0
    # A plain statement has an empty plan.
    r = sql(client, "SELECT count(*) FROM api.messages", dry_run=True)
    assert r.json()["rows"] == [] and r.json()["execution"]["plan"] == []


# --- ACL-8, QRY-1 ----------------------------------------------------------------


def test_acl_8_handles_bound_to_asker_in_sql(client, channel, fake_op):
    ids = seed(client, 2)
    add_channel(client, "fake.hidden")
    seed(client, 2, channel="fake.hidden")
    handle = resultsets.create(
        client_scope(),
        [dict(record_id=f"m:{i}") for i in ids],
        completeness="exhaustive",
    )
    access.register_asker("guest", "restricted")
    access.grant("guest", channel="synthetic.chat")
    agent = create_token("synthetic agent", kind="agent", askers=["guest"])["token"]
    headers = {"Authorization": "Bearer " + agent}
    r = sql(
        client, f"SELECT * FROM api.fakepass(input => '{handle}') f", headers=headers
    )
    assert r.status_code == 400 and "not found" in r.json()["detail"], r.text
    assert fake_op.runs == []
    r = sql(client, f"SELECT * FROM api.result_set('{handle}')", headers=headers)
    assert r.status_code == 200 and r.json()["rows"] == []
    # The asker's own recall sees only granted channels, and its set is its own.
    r = sql(
        client,
        "SELECT r.channel, r.record_id FROM api.recall(query => 'orchard', k => 10) r",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert {row["channel"] for row in r.json()["rows"]} == {"synthetic.chat"}
    own = r.json()["execution"]["semantic_calls"][0]["handle"]
    r = sql(client, f"SELECT count(*) AS n FROM api.result_set('{own}')")
    assert r.json()["rows"] == [{"n": 0}]


@pytest.mark.parametrize("escape", ESCAPES)
def test_qry_1_escapes_still_refused(client, recall_calls, escape):
    for statement in (
        "SELECT * FROM api.recall(query => 'orchard') r CROSS JOIN (" + escape + ") x",
        escape + "; SELECT * FROM api.recall(query => 'orchard') r",
        "WITH r AS (SELECT * FROM api.recall(query => 'orchard')) " + escape,
    ):
        r = sql(client, statement)
        assert r.status_code == 400, (statement, r.text)
    assert recall_calls.runs == []


# --- byte offsets, legacy shape ------------------------------------------------------


def test_qry_37_top_level_only(client, channel, recall_calls):
    """An operator marked top_level_only (librarian) stands alone in the
    top-level FROM: never joined, nested, in a CTE or beside another call."""
    op, calls = passthrough("faketop")
    op = SemanticOp(**dict(op.__dict__, top_level_only=True))
    semantic_ops.register(op)
    try:
        r = sql(client, "SELECT * FROM api.faketop() t")
        assert r.status_code == 200, r.text
        assert len(calls.runs) == 1
        for statement in (
            "SELECT * FROM api.faketop() t JOIN api.messages m ON true",
            "SELECT * FROM api.faketop() t, api.messages m",
            "WITH x AS (SELECT * FROM api.faketop()) SELECT * FROM x",
            "SELECT * FROM (SELECT * FROM api.faketop()) x",
            "SELECT * FROM api.messages m WHERE EXISTS (SELECT 1 FROM api.faketop())",
            "SELECT * FROM api.faketop() t UNION ALL SELECT * FROM api.recall('x') r",
        ):
            r = sql(client, statement)
            assert r.status_code == 400, (statement, r.text)
            assert "only allowed alone in the top-level FROM" in r.json()["detail"]
        assert len(calls.runs) == 1 and recall_calls.runs == []
    finally:
        semantic_ops.unregister(op.name)


def test_alias_and_byte_offsets(client, channel):
    seed(client, 3)
    call_text = (
        "api.recall(query => 'orchard )', k => 2,"
        """ options => '{"hints": {"note": "é ("}}')"""
    )
    statement = (
        "SELECT 'é(' AS x, r.record_id FROM "
        + call_text
        + " AS r WHERE r.record_id <> 'api.recall(x)'"
    )
    (call,) = semantic.find_calls(statement)
    raw = statement.encode("utf-8")
    assert raw[call.start : call.end].decode("utf-8") == call_text
    assert call.alias == "r" and call.args == {
        "query": "orchard )",
        "k": 2,
        "options": {"hints": {"note": "é ("}},
    }
    spliced = semantic.splice(statement, [(call, ZERO)])
    assert spliced == statement.replace(call_text, f"api.result_set('{ZERO}')")
    r = sql(client, statement)
    assert r.status_code == 200, r.text
    assert {row["x"] for row in r.json()["rows"]} == {"é("}


def test_query_legacy_shape_and_allowlist_has_no_semantic_names(client, channel):
    seed(client, 1)
    result = query("SELECT count(*) AS n FROM api.messages")
    assert set(result) == {"rows", "truncated", "row_count"}
    names = semantic_ops.names()
    assert "recall" in names
    assert not names & FUNCTIONS
    rows = owner("SELECT name,kind FROM tl.api_functions WHERE name='recall'")
    assert rows == [dict(name="recall", kind="semantic")]
    assert not owner(
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname='recall'"
    )


# --- episode grain --------------------------------------------------------------


def _thread_messages(channel, thread, offsets):
    return [
        message(
            0,
            channel=channel,
            external_id=f"{channel}-{thread}-{n}",
            thread={"external_id": thread},
            happened_at=(AT + timedelta(seconds=offset)).isoformat(),
            content=f"Synthetic orchard episode {thread} {n}",
            fields={},
        )
        for n, offset in enumerate(offsets)
    ]


def _ids(channel):
    rows = owner(
        "SELECT m.id,m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.happened_at,m.id",
        (channel,),
    )
    return [r["id"] for r in rows]


def test_p7_episodes_of_sessions_and_stable_ids(client):
    add_channel(client, "fake.chat", conversation_gap_s=1800)
    add_channel(client, "fake.whole", conversation_whole_thread=True)
    r = ingest(
        client,
        _thread_messages("fake.chat", "t1", [0, 60, 5000, 5060])
        + _thread_messages("fake.whole", "t2", [0, 100000]),
    )
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text
    chat, whole = _ids("fake.chat"), _ids("fake.whole")
    statement = (
        "SELECT * FROM api.episodes_of(ARRAY["
        + ",".join(map(str, chat + whole))
        + "]::bigint[]) ORDER BY message_id"
    )
    first = sql(client, statement)
    assert first.status_code == 200, first.text
    rows = {row["message_id"]: row for row in first.json()["rows"]}
    thread = rows[chat[0]]["thread_id"]
    assert (
        rows[chat[0]]["episode_id"]
        == rows[chat[1]]["episode_id"]
        == f"ep:{thread}:{chat[0]}"
    )
    assert (
        rows[chat[2]]["episode_id"]
        == rows[chat[3]]["episode_id"]
        == f"ep:{thread}:{chat[2]}"
    )
    assert (
        rows[chat[0]]["message_count"] == 2
        and rows[chat[2]]["first_message_id"] == chat[2]
    )
    assert rows[whole[0]]["episode_id"] == rows[whole[1]]["episode_id"]
    assert rows[whole[0]]["message_count"] == 2
    assert rows[whole[0]]["started_at"] < rows[whole[0]]["ended_at"]
    # Ids are identical across calls (and for a subset of the input).
    assert sql(client, statement).json()["rows"] == first.json()["rows"]
    subset = sql(client, f"SELECT * FROM api.episodes_of(ARRAY[{chat[3]}]::bigint[])")
    assert subset.json()["rows"][0]["episode_id"] == f"ep:{thread}:{chat[2]}"
    incidents = sql(
        client,
        "SELECT count(DISTINCT episode_id) AS n FROM api.episodes_of(ARRAY["
        + ",".join(map(str, chat + whole))
        + "]::bigint[])",
    )
    assert incidents.json()["rows"] == [{"n": 3}]
    too_many = sql(
        client,
        "SELECT * FROM api.episodes_of(ARRAY(SELECT generate_series(1,10001))::bigint[])",
    )
    assert too_many.status_code == 400 and too_many.json()["code"] == "54000"


def test_p7_episodes_of_restricted_asker_only_visible_rows(client):
    add_channel(client, "fake.open")
    add_channel(client, "fake.secret")
    r = ingest(
        client,
        _thread_messages("fake.open", "o1", [0, 10, 20])
        + _thread_messages("fake.secret", "s1", [0, 10]),
    )
    assert r.status_code == 200, r.text
    visible, hidden = _ids("fake.open"), _ids("fake.secret")
    access.register_asker("guest", "restricted")
    access.grant("guest", channel="fake.open")
    agent = create_token("synthetic agent", kind="agent", askers=["guest"])["token"]
    statement = (
        "SELECT * FROM api.episodes_of(ARRAY["
        + ",".join(map(str, visible + hidden))
        + "]::bigint[])"
    )
    r = sql(client, statement, headers={"Authorization": "Bearer " + agent})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert sorted(row["message_id"] for row in rows) == sorted(visible)
    assert {row["message_count"] for row in rows} == {3}
    r = sql(
        client,
        "SELECT sum(message_count) AS n, count(*) AS c FROM api.episodes_of(ARRAY["
        + ",".join(map(str, hidden))
        + "]::bigint[])",
        headers={"Authorization": "Bearer " + agent},
    )
    assert r.json()["rows"] == [{"n": None, "c": 0}]
    # The owner sees both threads.
    assert len(sql(client, statement).json()["rows"]) == 5


def test_p7_hit_count_warning(client, channel):
    seed(client, 4)
    r = sql(client, "SELECT count(*) AS n FROM api.search('orchard', NULL) s")
    execution = r.json()["execution"]
    assert GRAIN in execution["warning_codes"]
    assert any(w.startswith(GRAIN) and "episode_id" in w for w in execution["warnings"])
    assert AGGREGATE not in execution["warnings"]
    # A warning only, even under strict_exact.
    r = sql(
        client,
        "SELECT count(*) AS n FROM api.search('orchard', NULL) s",
        strict_exact=True,
    )
    assert r.status_code == 200 and r.json()["rows"] == [{"n": 4}]
    for statement in (
        "SELECT count(DISTINCT e.episode_id) AS n FROM api.episodes_of(ARRAY(SELECT id FROM api.search_filtered('orchard', NULL))) e",
        "SELECT count(DISTINCT e.episode_id) AS n FROM api.search_filtered('orchard', NULL) s"
        " JOIN api.episodes_of(ARRAY(SELECT id FROM api.messages)) e ON e.message_id = s.id",
        "SELECT e.episode_id, count(*) AS n FROM api.search_filtered('orchard', NULL) s"
        " JOIN api.episodes_of(ARRAY(SELECT id FROM api.messages)) e ON e.message_id = s.id GROUP BY e.episode_id",
    ):
        r = sql(client, statement)
        assert r.status_code == 200, r.text
        assert GRAIN not in (r.json()["execution"].get("warning_codes") or []), (
            statement
        )
    # Counting the hits of a recall set is a hit count too.
    r = sql(
        client,
        "SELECT count(*) AS n FROM api.recall(query => 'orchard', exhaustive => true) r",
    )
    assert r.status_code == 200, r.text
    assert GRAIN in r.json()["execution"]["warning_codes"]


def test_p7_grain_catalogue_section(client):
    r = sql(client, "SELECT api.describe('grain') AS d")
    body = r.json()["rows"][0]["d"]
    assert set(body["units"]) == {"record", "episode", "day", "conversation"}
    assert "a hit count is not an incident count" in body["description"].lower()
    assert "count(DISTINCT" in body["counting"] and "episode_id" in body["counting"]
    assert "bridges two sessions merges them" in body["stability"]
    for subject in ("semantic_sql", "plans", "envelope"):
        section = sql(client, f"SELECT api.describe('{subject}') AS d").json()["rows"][
            0
        ]["d"]
        assert section and section["description"], subject
    rules = sql(client, "SELECT api.describe('semantic_sql') AS d").json()["rows"][0][
        "d"
    ]
    assert "rejections" in rules and "taint" in rules and "examples" in rules
    rows = owner("SELECT * FROM tl.api_functions WHERE name='episodes_of'")
    assert rows[0]["kind"] == "sql" and rows[0]["exactness"] == "exact"
    assert rows[0]["cost_class"] == "free" and rows[0]["ranked_limit_arg"] is None
    assert "episodes_of" in FUNCTIONS
