"""Plans (operation DAGs under one budget) and evidence packets (migration
0040). Synthetic data only; every operator is local and deterministic."""

import threading
import time

import pytest
from conftest import ingest, message

from throughline import access, packets, plans, resultsets, semantic_ops
from throughline.db import connect, create_token
from throughline.retrieval.types import RECORD_KEYS
from throughline.scope import Scope
from throughline.semantic_ops import SemanticOp

ASKER = "fake-asker"


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
    return dict(
        owner(
            "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id DESC LIMIT 1"
        )[0],
        asker=None,
    )


def client_scope(asker=None):
    return Scope.from_token(dict(client_token(), asker=asker))


def seed(client, n=5, channel="synthetic.chat", start=0):
    extra = {} if channel == "synthetic.chat" else {"fields": {}}
    r = ingest(
        client,
        [
            message(i, channel=channel, external_id=f"{channel}-{i}", **extra)
            for i in range(start, start + n)
        ],
    )
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text
    rows = owner(
        "SELECT m.id,m.external_id,m.version FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL",
        (channel,),
    )
    found = {r["external_id"]: r for r in rows}
    return [found[f"{channel}-{i}"] for i in range(start, start + n)]


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []

    def add(self, **event):
        with self.lock:
            self.events.append(event)

    def by(self, node):
        return [e for e in self.events if e.get("note") == node]


def make_op(name, recorder, *, barrier=None, fail=None, sleep_s=0.0, records=()):
    """An operator that records when it ran, with what budget, and returns a
    set (its input, or the given records)."""

    def run(ctx, args):
        note = args.get("note")
        recorder.add(
            note=note,
            phase="start",
            at=time.perf_counter(),
            model_cost=ctx.tracker.budget.model_cost,
        )
        if barrier is not None and note in barrier[1]:
            barrier[0].wait(timeout=10)
        if sleep_s:
            time.sleep(sleep_s)
        if fail is not None and note in fail:
            raise RuntimeError("synthetic failure")
        recorder.add(note=note, phase="end", at=time.perf_counter())
        if args.get("input"):
            return args["input"]
        return resultsets.create(
            ctx.scope,
            [dict(record_id=r) for r in records],
            completeness="exhaustive",
            origin=dict(op=name),
        )

    return SemanticOp(
        name=name,
        params={"input": "handle", "note": "text"},
        input_params=("input",),
        exact=True,
        estimate=lambda ctx, args: [],
        run=run,
    )


@pytest.fixture()
def fake_ops(monkeypatch):
    """Registers test operators and admits them as plan ops."""
    registered = []

    def add(op):
        semantic_ops.register(op)
        registered.append(op.name)
        monkeypatch.setattr(
            plans, "SEMANTIC_PLAN_OPS", tuple(plans.SEMANTIC_PLAN_OPS) + (op.name,)
        )
        return op

    yield add
    for name in registered:
        semantic_ops.unregister(name)


def run_plan(nodes, **kwargs):
    return plans.execute(client_scope(), client_token(), dict(nodes=nodes), **kwargs)


# --- QRY-42, QRY-41 --------------------------------------------------------------


def test_qry_42_branches_concurrent_dependants_wait(client, channel, fake_ops):
    rows = seed(client, 3)
    recorder = Recorder()
    barrier = (threading.Barrier(2), {"a", "b"})
    fake_ops(
        make_op(
            "fakenode",
            recorder,
            barrier=barrier,
            records=[f"m:{r['id']}" for r in rows],
        )
    )
    result = run_plan(
        [
            dict(id="a", op="fakenode", args=dict(note="a")),
            dict(id="b", op="fakenode", args=dict(note="b")),
            dict(id="c", op="fakenode", args=dict(note="c"), inputs=dict(input="a")),
            dict(id="u", op="transform", args=dict(op="union"), inputs=["a", "b"]),
        ]
    )
    execution = result["execution"]
    assert execution["status"] == "complete", execution
    # a and b met at the barrier, so they ran at the same time.
    assert not barrier[0].broken
    a_end = recorder.by("a")[-1]["at"]
    c_start = recorder.by("c")[0]["at"]
    assert c_start >= a_end
    assert list(result["handles"]) == ["a", "b", "c", "u"]
    assert result["handles"]["c"] == result["handles"]["a"]
    assert result["previews"]["u"]["member_count"] == 3
    assert [n["status"] for n in execution["nodes"]] == ["ok"] * 4


def test_qry_41_budget_reserved_before_dispatch(client, channel, fake_ops):
    recorder = Recorder()
    fake_ops(make_op("fakebudget", recorder))
    result = run_plan(
        [
            dict(id="a", op="fakebudget", args=dict(note="a")),
            dict(id="b", op="fakebudget", args=dict(note="b")),
            dict(id="u", op="transform", args=dict(op="union"), inputs=["a", "b"]),
            dict(id="c", op="fakebudget", args=dict(note="c"), inputs=dict(input="u")),
        ],
        budgets=dict(model_cost=0.08),
    )
    assert result["execution"]["status"] == "complete"
    shares = {
        e["note"]: e["model_cost"] for e in recorder.events if e["phase"] == "start"
    }
    # Two ready branches share what is left; the dependant gets it all back.
    assert shares["a"] == pytest.approx(0.04) and shares["b"] == pytest.approx(0.04)
    assert shares["c"] == pytest.approx(0.08)
    assert result["execution"]["budget"]["limits"]["model_cost"] == pytest.approx(0.08)


def test_cst_10_plan_approval_covers_operators(client, channel, fake_ops):
    """A costly operator whose estimate passed the plan's cost gate is not
    gated again when it runs (an operator such as match authorizes itself
    only when nobody has), so an approved plan runs once and is then spent."""
    from throughline import cost
    from throughline.providers.base import Estimate

    runs = []

    def estimate(ctx, args):
        return [
            Estimate(
                purpose="fake",
                provider="fake",
                model="fake-model",
                calls=1,
                tokens=10,
                cost=1.0,
                local=False,
            )
        ]

    def run(ctx, args):
        if ctx.extras.get("authorized") is not True:
            cost.authorize(
                ctx.token_row, estimate(ctx, args), scope=ctx.scope, tracker=ctx.tracker
            )
        runs.append(args.get("note"))
        return resultsets.create(ctx.scope, [], completeness="exhaustive")

    fake_ops(
        SemanticOp(
            name="fakecostly",
            params={"input": "handle", "note": "text"},
            input_params=("input",),
            cost_class="model",
            estimate=estimate,
            run=run,
        )
    )
    nodes = [dict(id="a", op="fakecostly", args=dict(note="a"))]
    result = run_plan(nodes)
    execution = result["execution"]
    assert execution["status"] == "approval_required" and runs == []
    approval = execution["approval"]["approval_id"]
    with connect("app") as c:
        cost.decide(c, approval, "approved", by="synthetic owner")
    result = run_plan(nodes, approval_id=approval)
    assert result["execution"]["status"] == "complete", result["execution"]
    assert runs == ["a"] and list(result["handles"]) == ["a"]
    again = run_plan(nodes, approval_id=approval)
    assert again["execution"]["status"] == "approval_required" and runs == ["a"]


def test_plan_token_budget_shared_between_branches(client, channel, fake_ops):
    recorder = Recorder()
    seen = {}
    op = make_op("faketokens", recorder)

    def run(ctx, args):
        seen[args["note"]] = ctx.tracker.budget.tokens
        return op.run(ctx, args)

    fake_ops(SemanticOp(**dict(op.__dict__, run=run)))
    result = run_plan(
        [
            dict(id="a", op="faketokens", args=dict(note="a")),
            dict(id="b", op="faketokens", args=dict(note="b")),
            dict(id="c", op="faketokens", args=dict(note="c"), inputs=dict(input="a")),
        ],
        budgets=dict(tokens=1000),
    )
    assert result["execution"]["status"] == "complete"
    assert seen["a"] == seen["b"] == 500
    assert seen["c"] <= 1000


# --- validation, failure, dry run ---------------------------------------------------


def test_plan_validation(client, channel):
    cases = [
        (
            [
                dict(id="a", op="transform", args=dict(op="union"), inputs=["b"]),
                dict(id="b", op="transform", args=dict(op="union"), inputs=["a"]),
            ],
            "cycle",
        ),
        ([dict(id="a", op="nosuchop")], "unknown op"),
        ([dict(id="a", op="librarian", args=dict(goal="x"))], "not accepted"),
        (
            [dict(id=f"n{i}", op="recall", args=dict(query="x")) for i in range(25)],
            "at most 24",
        ),
        (
            [dict(id="a", op="sql", args=dict(statement="SELECT * FROM tl.messages"))],
            "relations must be explicitly in schema api",
        ),
        (
            [
                dict(
                    id="a",
                    op="sql",
                    args=dict(statement="SELECT * FROM api.recall('x') r"),
                )
            ],
            "semantic calls are not allowed",
        ),
        (
            [dict(id="a", op="recall", args=dict(query="x", nope=1))],
            "unknown parameter",
        ),
        ([dict(id="a", op="inspect", inputs=["zz"])], "unknown nodes"),
        (
            [
                dict(
                    id="a",
                    op="sql",
                    args=dict(statement="SELECT id FROM api.result_set({{zz}})"),
                )
            ],
            "unknown nodes",
        ),
    ]
    for nodes, text in cases:
        with pytest.raises(ValueError) as error:
            plans.validate(dict(nodes=nodes))
        assert text in str(error.value), (text, str(error.value))
        r = client.post("/v1/plans", json=dict(plan=dict(nodes=nodes)))
        assert r.status_code == 400 and text in r.json()["detail"], r.text
    assert owner("SELECT count(*) n FROM tl.result_sets")[0]["n"] == 0
    # hints.escalate is stripped from recall arguments.
    checked = plans.validate(
        dict(
            nodes=[
                dict(
                    id="a",
                    op="recall",
                    args=dict(query="x", options={"hints": {"escalate": True}}),
                )
            ]
        )
    )
    args = plans._args(checked, checked.nodes["a"], {})
    assert "escalate" not in args["options"]["hints"]


def test_branch_failure_isolated(client, channel, fake_ops):
    rows = seed(client, 2)
    recorder = Recorder()
    fake_ops(
        make_op(
            "fakefail",
            recorder,
            fail={"a"},
            records=[f"m:{r['id']}" for r in rows],
        )
    )
    result = run_plan(
        [
            dict(id="a", op="fakefail", args=dict(note="a")),
            dict(id="b", op="fakefail", args=dict(note="b")),
            dict(id="c", op="fakefail", args=dict(note="c"), inputs=dict(input="a")),
            dict(id="d", op="transform", args=dict(op="union"), inputs=["b", "c"]),
            dict(
                id="e",
                op="sql",
                args=dict(
                    statement="SELECT message_id FROM api.result_set({{b}})",
                    max_rows=10,
                ),
            ),
        ]
    )
    execution = result["execution"]
    assert execution["status"] == "degraded"
    status = {n["id"]: n["status"] for n in execution["nodes"]}
    assert status == dict(a="failed", b="ok", c="skipped", d="skipped", e="ok")
    assert list(result["handles"]) == ["b", "e"]
    assert result["previews"]["e"]["member_count"] == 2
    assert result["previews"]["e"]["completeness"] == "exhaustive"
    assert recorder.by("c") == []


def test_plan_dry_run(client, channel, fake_ops):
    recorder = Recorder()
    fake_ops(make_op("fakedry", recorder))
    result = run_plan(
        [
            dict(id="a", op="fakedry", args=dict(note="a")),
            dict(id="r", op="recall", args=dict(query="orchard")),
        ],
        dry_run=True,
    )
    assert result["handles"] == {} and result["previews"] == {}
    execution = result["execution"]
    assert execution["dry_run"] is True
    assert [n["id"] for n in execution["plan"]["nodes"]] == ["a", "r"]
    assert execution["estimate"]["cost"] == 0
    assert recorder.events == []
    assert owner("SELECT count(*) n FROM tl.result_sets")[0]["n"] == 0
    r = client.post(
        "/v1/plans",
        json=dict(
            plan=dict(nodes=[dict(id="r", op="recall", args=dict(query="x"))]),
            dry_run=True,
        ),
    )
    assert r.status_code == 200 and r.json()["execution"]["dry_run"] is True


# --- LIB-9, QRY-43 ---------------------------------------------------------------


def test_lib_9_packet_field_list(client, channel):
    rows = seed(client, 3)
    handle = resultsets.create(
        client_scope(),
        [dict(record_id=f"m:{r['id']}") for r in rows],
        completeness="exhaustive",
    )
    note = dict(
        generated_text=f"Message m:{rows[0]['id']} mentions an orchard.",
        supporting_refs=[
            dict(record_id=f"m:{rows[0]['id']}", version=rows[0]["version"])
        ],
        uncertainty="low",
    )
    packet = packets.build(
        client_scope(), handle, notes=[note], unresolved=["who planted it"]
    )
    assert set(packets.FIELDS) <= set(packet)
    assert (
        packet["result_set_ref"] == handle and packet["execution_status"] == "complete"
    )
    assert len(packet["records"]) == 3
    for record in packet["records"]:
        assert set(packets.RECORD_FIELDS) <= set(record)
        assert set(record["applicable_time"]) == {"from", "to", "precisions"}
        assert record["revision_id"] == dict(
            message_id=int(record["record_id"][2:]), version=1
        )
        assert record["original_excerpt"].startswith("Synthetic orchard message")
        assert "evidence_class" in record and "attribution" in record
        for key in ("event_time", "recorded_time", "ingested_time"):
            assert record[key] is not None
    (entry,) = packet["orientation"]
    assert entry["generated"] is True and entry["supporting_refs"][0]["version"] == 1
    assert packet["unresolved_needs"] == ["who planted it"]
    coverage = packet["coverage"]
    assert set(coverage) >= {
        "authorized_scope",
        "retrieval_methods",
        "enumerated_or_sampled",
        "processing_counts",
        "known_source_gaps",
    }
    assert coverage["enumerated_or_sampled"] == "enumerated"
    assert packet["evidence_adequacy"]["records_delivered"] == 3
    assert packet["continuation_ref"] is None and packet["manifest"] is None
    # Note text never lands in a record field.
    assert "mentions an orchard" not in str(packet["records"])


def test_qry_43_note_rules(client, channel):
    client.post("/v1/channels", json=dict(name="fake.hidden", plugin="synthetic"))
    visible = seed(client, 2)
    (hidden,) = seed(client, 1, channel="fake.hidden")
    access.register_asker(ASKER, "restricted")
    access.grant(ASKER, channel="synthetic.chat")
    scope = client_scope(ASKER)
    members = [
        dict(record_id=f"m:{r['id']}", version=r["version"]) for r in visible + [hidden]
    ]
    handle = resultsets.create(scope, members, completeness="exhaustive")
    a, b = (f"m:{r['id']}" for r in visible)
    h = f"m:{hidden['id']}"

    def ref(record_id, version=1):
        return dict(record_id=record_id, version=version)

    owner(
        "INSERT INTO tl.record_sources(prefix,relation) VALUES ('zz','api.record_versions')"
    )
    try:
        notes = [
            dict(generated_text="kept", supporting_refs=[ref(a), ref(b)]),
            dict(generated_text="no refs", supporting_refs=[]),
            dict(generated_text="hidden member", supporting_refs=[ref(h)]),
            dict(
                generated_text=f"invented m:999999 near {a}", supporting_refs=[ref(a)]
            ),
            dict(generated_text="later prefix zz:5", supporting_refs=[ref(a)]),
            dict(generated_text="wrong version", supporting_refs=[ref(a, 7)]),
            dict(generated_text="no version", supporting_refs=[dict(record_id=a)]),
            dict(
                generated_text=f"names {b} which is delivered", supporting_refs=[ref(a)]
            ),
        ]
        packet = packets.build(scope, handle, notes=notes)
    finally:
        owner("DELETE FROM tl.record_sources WHERE prefix='zz'")
    assert [r["record_id"] for r in packet["records"]] == [a, b]
    assert [n["generated_text"] for n in packet["orientation"]] == [
        "kept",
        f"names {b} which is delivered",
    ]
    dropped = [o for o in packet["truncation_and_omissions"] if o["kind"] == "note"]
    assert [o["index"] for o in dropped] == [1, 2, 3, 4, 5, 6]
    reasons = {o["index"]: o for o in dropped}
    assert reasons[1]["reason"] == "no supporting reference"
    assert reasons[2]["ref"] == h
    assert reasons[3]["ref"] == "m:999999"
    assert reasons[4]["ref"] == "zz:5"
    assert "version" in reasons[5]["reason"] and "version" in reasons[6]["reason"]
    assert packet["evidence_adequacy"]["notes_dropped"] == 6
    assert h not in str(packet["records"]) and h not in str(packet["orientation"])


def test_packet_manifest_when_over_budget(client, channel):
    rows = seed(client, 6)
    handle = resultsets.create(
        client_scope(),
        [dict(record_id=f"m:{r['id']}") for r in rows],
        completeness="exhaustive",
    )
    packet = packets.build(client_scope(), handle, token_budget=400)
    kept = len(packet["records"])
    assert 0 < kept < 6
    assert [m["record_id"] for m in packet["manifest"]] == [
        f"m:{r['id']}" for r in rows[kept:]
    ]
    assert packet["continuation_ref"] == dict(
        result_set=handle, offset=kept, investigation_id=None
    )
    (omission,) = packet["truncation_and_omissions"]
    assert omission["reason"] == "token_budget" and omission["omitted"] == 6 - kept
    # Over HTTP the token's limit also applies, and the packet is the same.
    r = client.post("/v1/packets", json=dict(handle=handle, token_budget=400))
    assert r.status_code == 200 and len(r.json()["records"]) == kept
    assert (
        client.post("/v1/packets", json=dict(handle="rs_" + "1" * 32)).status_code
        == 404
    )


# --- LIB-5, INT-10 -----------------------------------------------------------------


def test_lib_5_external_agent_executes_and_packages_over_http(client, channel):
    seed(client, 5)
    agent = create_token("synthetic agent", kind="agent")["token"]
    headers = {"Authorization": "Bearer " + agent}
    plan = dict(
        nodes=[
            dict(
                id="hits",
                op="recall",
                args=dict(query="orchard", k=4, strategy="keyword"),
            ),
            dict(
                id="look", op="inspect", args=dict(view="by_channel"), inputs=["hits"]
            ),
            dict(
                id="first",
                op="sql",
                args=dict(
                    statement="SELECT message_id FROM api.result_set({{hits}}) ORDER BY ordinal LIMIT 2",
                    max_rows=10,
                ),
            ),
        ]
    )
    r = client.post("/v1/plans", json=dict(plan=plan), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["execution"]["status"] == "complete", body["execution"]["incomplete"]
    assert set(body["handles"]) == {"hits", "first"}
    assert body["previews"]["look"]["view"] == "by_channel"
    assert body["previews"]["first"]["member_count"] == 2
    assert body["previews"]["first"]["completeness"] == "ranked_sample"
    first = body["handles"]["first"]
    rows = client.get(f"/v1/sets/{first}", headers=headers).json()["records"]
    note = dict(
        generated_text="Two orchard messages.",
        supporting_refs=[
            dict(record_id=rows[0]["record_id"], version=rows[0]["version"])
        ],
    )
    r = client.post(
        "/v1/packets", json=dict(handle=first, notes=[note]), headers=headers
    )
    assert r.status_code == 200, r.text
    packet = r.json()
    assert [x["record_id"] for x in packet["records"]] == [x["record_id"] for x in rows]
    assert len(packet["orientation"]) == 1
    # The plan's handles belong to the agent, not to the owner token.
    assert client.post("/v1/packets", json=dict(handle=first)).status_code == 404
    audit = owner(
        "SELECT detail FROM tl.audit_log WHERE statement='POST /v1/plans' ORDER BY id DESC LIMIT 1"
    )[0]["detail"]
    assert audit["operation"] == "plan" and "orchard" not in str(audit)


def test_int_10_same_record_keys_across_recall_sets_plans_packets(client, channel):
    seed(client, 4)
    recall = client.post(
        "/v1/recall", json=dict(query="orchard", k=3, save=True)
    ).json()
    recall_keys = set(recall["records"][0])
    assert set(RECORD_KEYS) <= recall_keys
    from_set = client.get(f"/v1/sets/{recall['result_set']}").json()["records"]
    plan = client.post(
        "/v1/plans",
        json=dict(
            plan=dict(
                nodes=[dict(id="r", op="recall", args=dict(query="orchard", k=3))]
            )
        ),
    ).json()
    from_plan = client.get(f"/v1/sets/{plan['handles']['r']}").json()["records"]
    sql = client.post(
        "/v1/sql",
        json=dict(statement="SELECT * FROM api.recall(query => 'orchard', k => 3) r"),
    ).json()
    from_sql = client.get(
        f"/v1/sets/{sql['execution']['semantic_calls'][0]['handle']}"
    ).json()["records"]
    keys = [set(r) for r in from_set + from_plan + from_sql]
    assert all(k == keys[0] for k in keys)
    assert set(RECORD_KEYS) <= keys[0]
    assert recall_keys - {"ordinal", "meta"} <= keys[0]
    ids = [r["record_id"] for r in recall["records"]]
    assert [r["record_id"] for r in from_plan] == ids
    assert [r["record_id"] for r in from_sql] == ids
    packet = client.post("/v1/packets", json=dict(handle=plan["handles"]["r"])).json()
    assert [r["record_id"] for r in packet["records"]] == ids


# --- optional feature: expand and match (semantic operators) ----------------------------


def test_wave_expand_match_sql_forms_plans_and_int_10_keys(client, channel):
    available = semantic_ops.names()
    if not {"expand", "match"} <= available:
        pytest.skip("expand and match operators are not installed")
    seed(client, 4)
    expand = client.post(
        "/v1/sql",
        json=dict(
            statement="SELECT e.record_id, e.role FROM api.expand(input_sql =>"
            " 'SELECT id FROM api.messages ORDER BY id LIMIT 1', max_rows => 5,"
            " mode => 'context', before => 1, after => 1) e"
        ),
    )
    assert expand.status_code == 200, expand.text
    (call,) = expand.json()["execution"]["semantic_calls"]
    assert call["name"] == "expand" and call["completeness"] is not None
    assert expand.json()["row_count"] >= 1
    match = client.post(
        "/v1/sql",
        json=dict(
            statement="SELECT m.record_id, m.meta FROM api.match(condition =>"
            " 'mentions an orchard', input_sql => 'SELECT id FROM api.messages',"
            " max_rows => 10) m"
        ),
    )
    assert match.status_code == 200, match.text
    assert match.json()["execution"]["status"] in ("complete", "degraded")
    plan = client.post(
        "/v1/plans",
        json=dict(
            plan=dict(
                nodes=[
                    dict(
                        id="hits",
                        op="recall",
                        args=dict(query="orchard", k=2, strategy="keyword"),
                    ),
                    dict(
                        id="ctx",
                        op="expand",
                        args=dict(mode="context", before=1, after=1),
                        inputs=dict(input="hits"),
                    ),
                    dict(
                        id="judged",
                        op="match",
                        args=dict(condition="mentions an orchard", max_rows=10),
                        inputs=dict(input="ctx"),
                    ),
                ]
            )
        ),
    )
    assert plan.status_code == 200, plan.text
    handles = plan.json()["handles"]
    assert list(handles) == ["hits", "ctx", "judged"], plan.json()["execution"]
    keys = []
    for handle in [
        call["handle"],
        match.json()["execution"]["semantic_calls"][0]["handle"],
        *handles.values(),
    ]:
        records = client.get(f"/v1/sets/{handle}").json()["records"]
        assert records, handle
        keys += [set(r) for r in records]
    assert all(k == keys[0] for k in keys)
    assert set(RECORD_KEYS) <= keys[0]
    packet = client.post("/v1/packets", json=dict(handle=handles["judged"])).json()
    for record in packet["records"]:
        assert set(packets.RECORD_FIELDS) <= set(record)
