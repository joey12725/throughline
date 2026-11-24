"""Evidence selection, dedup, grouping and the adaptive planner (selection).

Synthetic data only; no model credentials (deterministic local providers).
"""

import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message

from throughline import operators
from throughline.db import connect
from throughline.query import query
from throughline.retrieval import RecallRequest, recall, selection
from throughline.retrieval import legs as legs_module
from throughline.retrieval import planner as planner_module
from throughline.retrieval import stages as stages_module
from throughline.retrieval.legs import Leg
from throughline.retrieval.stages import Stage
from throughline.retrieval.types import Candidate
from throughline.scope import Scope

select_stage = importlib.import_module("throughline.retrieval.stages.select")
recall_module = importlib.import_module("throughline.retrieval.recall")

OWNER = Scope.local()
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
LONG = "with enough surrounding words that this message is clearly not a short one"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_") and any(
            part in name
            for part in (
                "EMBEDDING_",
                "RERANK_",
                "SEMANTIC_",
                "CONTROLLER_",
                "MODEL_",
                "EGRESS_",
                "RRF_K",
                "RECALL_",
                "RETRIEVAL_",
                "LEG_TIMEOUT",
                "DEP_SHORT",
                "IDENTITY_QUALITY",
                "MIN_ROUND_GAIN",
                "RUNTIME_PROFILES",
                "DEFAULT_RANKER",
            )
        ):
            monkeypatch.delenv(name)
    # Runtime profile rows would change implementation choices between the
    # recalls of one test; the test about them turns them back on.
    monkeypatch.setenv("THROUGHLINE_RUNTIME_PROFILES", "0")
    planner_module.PLANNER.reset_profile_clock()


@pytest.fixture()
def extra():
    added = []

    def leg(obj):
        legs_module.register(obj)
        added.append((legs_module, obj.name))
        return obj

    def stage(obj):
        stages_module.register(obj)
        added.append((stages_module, obj.name))
        return obj

    leg.stage = stage
    yield leg
    for module, name in added:
        module.unregister(name)
    for spec in operators.registered():
        if spec.name.startswith("leg.fake_"):
            operators.unregister(spec.name)


def run(who=OWNER, **kwargs):
    return recall(who, RecallRequest(**kwargs))


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def ids(channel, external_ids):
    rows = owner(
        "SELECT m.id, m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL",
        (channel,),
    )
    found = {r["external_id"]: r["id"] for r in rows}
    return [found[e] for e in external_ids]


def stage(response, name):
    return [s for s in response.execution["stages"] if s["name"] == name]


def message_ids(response, role=None):
    return [
        r["message_id"]
        for r in response.records
        if r["record_id"].startswith("m:") and (role is None or r["role"] == role)
    ]


def profile(operator, implementation, p95, quality=None, source="bench"):
    from psycopg.types.json import Jsonb

    owner(
        "INSERT INTO tl.operator_profiles(operator,implementation,p50_ms,p95_ms,quality,source) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            operator,
            implementation,
            p95,
            p95,
            Jsonb(quality) if quality is not None else None,
            source,
        ),
    )


def fake(record_id, score, words="", thread=1, channel="c", when=AT, **kwargs):
    """A candidate with record facts as the stages would load them."""
    kind = kwargs.pop("kind", "life_event")
    generated = kwargs.pop("generated", kind not in ("life_event", "reference"))
    tokens = kwargs.pop("tokens", 20)
    candidate = Candidate(
        record_id=record_id,
        kind=kind,
        score=score,
        thread_id=thread,
        thread_no=kwargs.pop("thread_no", None),
        generated=generated,
        support=kwargs.pop("support", []),
        tokens=tokens,
        **kwargs,
    )
    if record_id.startswith("m:"):
        candidate.message_id = int(record_id[2:])
    candidate.meta["facts"] = dict(
        kind=kind,
        channel=channel,
        record_time=when,
        terms=sorted(selection.terms(words)),
        lead=selection.leading_words(words),
        tokens=tokens,
    )
    return candidate


def link(type_, from_id, to_id):
    with connect() as c:
        from throughline.ingest import add_link

        return add_link(
            c,
            {
                "type": type_,
                "from": {"kind": "message", "id": from_id},
                "to": {"kind": "message", "id": to_id},
                "method": "owner",
            },
        )["id"]


# --- QRY-24 joint selection --------------------------------------------------------


def test_qry_24_joint_selection_beats_topk_on_coverage():
    query_text = "apple banana cherry"
    candidates = [
        fake("m:1", 1.0, "apple banana pie", thread=1),
        fake("m:2", 0.9, "apple banana bread", thread=2),
        fake("m:3", 0.7, "cherry tart", thread=3),
    ]
    top_k = candidates[:2]
    chosen, dropped = selection.select(candidates, None, 2, query=query_text)
    assert [c.record_id for c in chosen] == ["m:1", "m:3"]
    covered = set().union(*(set(c.meta["facts"]["terms"]) for c in chosen))
    top_covered = set().union(*(set(c.meta["facts"]["terms"]) for c in top_k))
    wanted = selection.terms(query_text)
    assert len(covered & wanted) == 3 > len(top_covered & wanted)
    assert dropped == [dict(record_id="m:2", reason="rank")]


def test_qry_24_budget_never_exceeded_and_drops_reported(client, channel):
    texts = [f"orchard report number {i} {LONG}" for i in range(12)]
    assert (
        ingest(
            client,
            [
                message(i * 4000, content=t, thread={"external_id": f"fake-{i}"})
                for i, t in enumerate(texts)
            ],
        ).status_code
        == 200
    )
    response = run(query="orchard", k=10, budgets=dict(tokens=80))
    assert stage(response, "select")[0]["status"] == "ok"
    assert 0 < len(response.records) < 10
    assert sum(r["tokens"] for r in response.records) <= 80
    reasons = {d["reason"] for d in response.dropped}
    assert "budget" in reasons and reasons <= set(selection.DROP_REASONS)
    assert response.execution["truncation"]["reason"] == "token_budget"
    counts = response.execution["selection"]
    assert counts["spent_tokens"] <= 80 and counts["dropped"]["budget"] >= 1
    # Pure: dependencies count against the budget too.
    hit = fake("m:10", 1.0, "short", tokens=5)
    dep = fake("m:9", None, "context", tokens=30)
    chosen, dropped = selection.select([hit], 20, 5, deps={"m:10": [dep]})
    assert chosen == [] and dropped[0]["reason"] == "budget"
    assert dropped[0]["with_dependencies"] == ["m:9"]


# --- QRY-26 provenance-aware groups ----------------------------------------------------


def test_qry_26_page_passage_and_raw_source_one_group():
    raw = fake("m:1", 1.0, kind="life_event")
    page = fake(
        "m:2",
        0.9,
        kind="page_revision",
        support=[dict(message_id=1, version=1, relation="source")],
    )
    passage = fake(
        "p:3",
        0.8,
        kind="passage",
        support=[dict(message_id=1, version=1, relation="source")],
    )
    groups = selection.independent_groups([raw, page, passage])
    assert len({g["group"] for g in groups.values()}) == 1
    assert {g["independent_sources"] for g in groups.values()} == {1}
    assert selection.independent_source_count([raw, page, passage]) == 1
    # Two unrelated messages are two independent sources.
    other = fake("m:4", 0.5, thread=2)
    assert selection.independent_source_count([raw, other]) == 2
    # An unsupported page adds nothing and is flagged.
    lonely = fake("m:6", 0.4, kind="page_revision", support=[])
    groups = selection.independent_groups([raw, other, lonely])
    assert groups["m:6"]["unsupported"] is True
    assert groups["m:6"]["independent_sources"] == 0
    assert selection.independent_source_count([raw, other, lonely]) == 2
    assert groups["m:1"]["unsupported"] is False


def test_qry_26_dedup_annotates_recall_candidates(client, channel, extra):
    seen = {}

    class Capture(Stage):
        name = "fake_capture"
        order = 35

        def run(self, ctx, candidates):
            for c in candidates:
                seen[c.record_id] = dict(c.meta)
            return candidates

    extra.stage(Capture())
    assert ingest(client, [message(0), message(1)]).status_code == 200
    run(query="orchard", k=5)
    assert seen and all(m["independent_sources"] == 1 for m in seen.values())
    assert len({m["independent_group"] for m in seen.values()}) == 2


# --- QRY-27 context dependencies --------------------------------------------------------


def _dialogue(client, extra_messages=()):
    long_text = "We planted the new orchard row beside the old stone wall " + LONG
    assert (
        ingest(
            client,
            [
                message(0, content=long_text),
                message(1, content="yes, the pears"),
                *extra_messages,
            ],
        ).status_code
        == 200
    )
    return ids("synthetic.chat", ["fake-0", "fake-1"])


def test_qry_27_dependency_with_role_within_budget(client, channel):
    first, reply = _dialogue(client)
    response = run(query="pears", strategy="keyword", k=5, budgets=dict(tokens=500))
    roles = [(r["message_id"], r["role"], r["depends_on"]) for r in response.records]
    assert roles == [(first, "dependency", f"m:{reply}"), (reply, "hit", None)]
    assert sum(r["tokens"] for r in response.records) <= 500
    # Too small for the pair: both are left out, together.
    tight = run(query="pears", strategy="keyword", k=5, budgets=dict(tokens=10))
    assert tight.records == []
    dropped = [d for d in tight.dropped if d["record_id"] == f"m:{reply}"]
    assert dropped and dropped[0]["reason"] == "budget"
    assert dropped[0]["with_dependencies"] == [f"m:{first}"]


def test_qry_27_dependency_respects_reduced_scope(client, channel):
    # The neighbour is ingested after the cutoff: hidden by known_by.
    assert ingest(client, [message(1, content="yes, the pears")]).status_code == 200
    time.sleep(0.05)
    cutoff = owner("SELECT clock_timestamp() AS t")[0]["t"]
    time.sleep(0.05)
    neighbour = "We planted the new orchard row beside the old stone wall " + LONG
    assert ingest(client, [message(0, content=neighbour)]).status_code == 200
    (reply,) = ids("synthetic.chat", ["fake-1"])
    later = run(query="pears", strategy="keyword", k=5, budgets=dict(tokens=500))
    assert len(later.records) == 2
    hidden = run(
        query="pears",
        strategy="keyword",
        k=5,
        budgets=dict(tokens=500),
        scope=dict(known_by=cutoff.isoformat()),
    )
    assert message_ids(hidden) == [reply]
    # A neighbour by another person may be a dependency, never a hit.
    p = owner("INSERT INTO tl.people(handle) VALUES ('synthetic-p') RETURNING id")[0][
        "id"
    ]
    q = owner("INSERT INTO tl.people(handle) VALUES ('synthetic-q') RETURNING id")[0][
        "id"
    ]
    first, reply = ids("synthetic.chat", ["fake-0", "fake-1"])
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (p, reply))
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (q, first))
    scoped = run(
        query="pears",
        k=5,
        budgets=dict(tokens=500),
        scope=dict(persons=[p]),
    )
    assert [(r["message_id"], r["role"]) for r in scoped.records] == [
        (first, "dependency"),
        (reply, "hit"),
    ]
    assert all(r["person_id"] == p for r in scoped.records if r["role"] == "hit")


def test_qry_27_reply_link_dependency(client, channel):
    texts = [f"orchard notes {LONG}", f"plum notes {LONG}"]
    assert (
        ingest(
            client,
            [
                message(0, content=texts[0], thread={"external_id": "fake-a"}),
                message(9000, content=texts[1], thread={"external_id": "fake-b"}),
            ],
        ).status_code
        == 200
    )
    target, answer = ids("synthetic.chat", ["fake-0", "fake-9000"])
    link("reply_to", answer, target)
    response = run(query="plum", strategy="keyword", k=5, budgets=dict(tokens=500))
    assert [(r["message_id"], r["role"]) for r in response.records] == [
        (target, "dependency"),
        (answer, "hit"),
    ]


def test_qry_27_dependency_hidden_by_kinds_or_as_of(client, channel):
    r = client.post(
        "/v1/channels",
        json=dict(name="fake.reference", plugin="synthetic", is_reference=True),
    )
    assert r.status_code == 200, r.text
    assert (
        ingest(
            client,
            [
                message(
                    0,
                    channel="fake.reference",
                    content=f"orchard handbook {LONG}",
                    thread={"external_id": "fake-ref"},
                    fields={},
                ),
                message(9000, content=f"plum notes {LONG}"),
            ],
        ).status_code
        == 200
    )
    (target,) = ids("fake.reference", ["fake-0"])
    (answer,) = ids("synthetic.chat", ["fake-9000"])
    link("reply_to", answer, target)
    request = dict(query="plum", strategy="keyword", k=5, budgets=dict(tokens=500))
    # Reference records are outside the default kinds: no dependency.
    plain = run(**request)
    assert [(r["message_id"], r["role"]) for r in plain.records] == [(answer, "hit")]
    both = ["life_event", "reference"]
    linked = run(**request, scope=dict(kinds=both))
    assert [(r["message_id"], r["role"]) for r in linked.records] == [
        (target, "dependency"),
        (answer, "hit"),
    ]
    # kinds is part of the reduced filter: a reference target is not added
    # when the request names only life events.
    narrowed = run(**request, scope=dict(kinds=["life_event"]))
    assert [(r["message_id"], r["role"]) for r in narrowed.records] == [(answer, "hit")]
    # as_of is too: a version written later is not context at that time.
    cutoff = owner("SELECT clock_timestamp() AS t")[0]["t"]
    time.sleep(0.05)
    assert (
        ingest(
            client,
            [
                message(
                    0,
                    channel="fake.reference",
                    content=f"orchard handbook revised {LONG}",
                    thread={"external_id": "fake-ref"},
                    fields={},
                )
            ],
        ).status_code
        == 200
    )
    (current,) = ids("fake.reference", ["fake-0"])
    assert current != target
    past = run(**request, scope=dict(as_of=cutoff.isoformat(), kinds=both))
    # The link now points at the new version, which did not exist at as_of:
    # it is not added as context.
    assert [(r["message_id"], r["role"]) for r in past.records] == [(answer, "hit")]


def test_qry_24_dependency_that_is_a_hit_respects_k():
    reply = fake("m:2", 1.0, "yes", thread=1, thread_no=2, tokens=3)
    first = fake("m:1", 0.9, "orchard", thread=1, thread_no=1)
    as_dep = fake("m:1", None, "orchard", thread=1, thread_no=1)
    chosen, dropped = selection.select([reply, first], None, 1, deps={"m:2": [as_dep]})
    assert [(c.record_id, c.role) for c in chosen] == [
        ("m:1", "dependency"),
        ("m:2", "hit"),
    ]
    assert dropped == []
    both, _ = selection.select(
        [
            fake("m:2", 1.0, "yes", thread=1, thread_no=2, tokens=3),
            fake("m:1", 0.9, "orchard", thread=1, thread_no=1),
        ],
        None,
        2,
        deps={"m:2": [fake("m:1", None, "orchard", thread=1, thread_no=1)]},
    )
    assert [c.role for c in both] == ["hit", "hit"]


def test_qry_27_opt_out(client, channel):
    _dialogue(client)
    response = run(
        query="pears", strategy="keyword", k=5, budgets=dict(tokens=500), expand=False
    )
    assert [r["role"] for r in response.records] == ["hit"]
    assert stage(response, "select")[0]["status"] == "ok"


# --- QRY-28 diversity ---------------------------------------------------------------------


def test_qry_28_diversity_soft_no_quota():
    def pool():
        return [
            fake("m:1", 1.0, "orchard", thread=1),
            fake("m:2", 0.99, "orchard", thread=1),
            fake("m:3", 0.98, "orchard", thread=1),
            fake("m:4", 0.7, "orchard", thread=2),
        ]

    diverse, _ = selection.select(pool(), None, 3, query="orchard")
    assert "m:4" in [c.record_id for c in diverse]
    flat, _ = selection.select(pool(), None, 3, query="orchard", diversity=False)
    assert [c.record_id for c in flat] == ["m:1", "m:2", "m:3"]
    # No quota: one conversation can fill the selection when nothing else is
    # there.
    alone, _ = selection.select(pool()[:3], None, 3, query="orchard")
    assert len(alone) == 3


# --- QRY-30 grouping ----------------------------------------------------------------------


def test_qry_30_group_by_manifest_reaches_response(client, channel):
    assert (
        ingest(
            client,
            [message(i, thread={"external_id": "fake-a"}) for i in range(3)]
            + [message(i, thread={"external_id": "fake-b"}) for i in range(3, 5)],
        ).status_code
        == 200
    )
    response = run(query="orchard", k=10, group_by="thread")
    manifest = response.manifest
    assert manifest["by"] == "thread" and manifest["unit"] == "record"
    assert manifest["records"] == 5 and manifest["incidents"] is None
    assert sorted(g["count"] for g in manifest["groups"]) == [2, 3]
    assert manifest["omitted"] == 0
    for record in response.records:
        assert record["groups"] == [f"thread:{record['thread_id']}"]
        group = next(g for g in manifest["groups"] if g["key"] == record["groups"][0])
        assert record["record_id"] in group["record_ids"]
    # Candidates cut by k are counted as omitted, not grouped.
    small = run(query="orchard", k=2, group_by="channel")
    assert small.manifest["records"] == 2 and small.manifest["omitted"] == 3
    # Over HTTP the manifest is part of the response.
    r = client.post("/v1/recall", json=dict(query="orchard", k=10, group_by="day"))
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["groups"][0]["key"] == "day:2020-01-01"


# --- QRY-15 / QRY-21 planner ----------------------------------------------------------------


class Fallback(Leg):
    name = "fake_fallback"
    calls = 0

    def __init__(self, ref=None):
        self.ref = ref

    def run(self, ctx):
        type(self).calls += 1
        if self.ref is None:
            return []
        return [Candidate(record_id=f"m:{self.ref}", message_id=self.ref, score=0.5)]


def test_qry_15_planner_skips_unavailable_and_slow_legs(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    assert recall_module.planner() is planner_module.PLANNER
    profile("leg.entity", "trigram_links", 900000)
    response = run(query="orchard", k=10, budgets=dict(deadline_ms=20000))
    assert stage(response, "vector")[0]["status"] == "skipped"
    assert stage(response, "vector")[0]["reason"] == "embeddings_unavailable"
    assert stage(response, "entity")[0]["status"] == "pruned"
    assert stage(response, "entity")[0]["reason"] == "p95_exceeds_deadline"
    assert stage(response, "planner")[0]["legs"][0] == "lexical"
    assert len(response.records) == 3
    # A query of stop words plans no keyword or meaning leg.
    stop = run(query="the of and", k=10)
    assert stage(stop, "lexical")[0]["reason"] == "empty_tsquery"
    assert "lexical" not in stage(stop, "planner")[0]["legs"]
    # Explicit legs are an override and are not pruned.
    chosen = run(query="orchard", legs=["entity", "lexical"], k=10)
    assert stage(chosen, "planner")[0]["legs"] == ["entity", "lexical"]


def test_qry_15_second_round_only_with_trigger(client, channel, extra, monkeypatch):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    (extra_id,) = ids("synthetic.chat", ["fake-2"])
    Fallback.calls = 0
    extra(Fallback(extra_id))
    enough = run(query="orchard", k=2)
    planner = stage(enough, "planner")[0]
    assert planner["rounds"] == 1 and planner["stop_reason"] == "enough_candidates"
    assert Fallback.calls == 0
    fewer = run(query="orchard", k=10)
    planner = stage(fewer, "planner")[0]
    assert planner["rounds"] == 2 and "fake_fallback" in planner["legs"]
    assert stage(fewer, "fake_fallback")[0]["round"] == 2
    assert Fallback.calls == 1
    # A request that names its legs gets exactly one round.
    named = run(query="orchard", legs=["lexical"], k=10)
    assert stage(named, "planner")[0]["stop_reason"] == "single_round"


def test_qry_21_stops_when_round_adds_nothing_and_reports_reason(
    client, channel, extra, monkeypatch
):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    (known,) = ids("synthetic.chat", ["fake-0"])
    extra(Fallback(known))
    monkeypatch.setenv("THROUGHLINE_RECALL_MAX_ROUNDS", "3")
    response = run(query="orchard", k=10)
    planner = stage(response, "planner")[0]
    assert planner["rounds"] == 2 and planner["stop_reason"] == "no_new_candidates"
    # A small shortfall is not worth a round.
    monkeypatch.setenv("THROUGHLINE_MIN_ROUND_GAIN", "0.9")
    response = run(query="orchard", k=4)
    planner = stage(response, "planner")[0]
    assert planner["rounds"] == 1 and planner["stop_reason"] == "low_expected_gain"
    # Without fallback legs the planner says so.
    legs_module.unregister("fake_fallback")
    monkeypatch.delenv("THROUGHLINE_MIN_ROUND_GAIN")
    response = run(query="orchard", k=10)
    assert stage(response, "planner")[0]["stop_reason"] == "no_more_legs"


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_qry_21_no_sufficiency_flag(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    for response in (
        run(query="orchard", k=2),
        run(query="orchard", k=10, budgets=dict(tokens=100), group_by="thread"),
    ):
        assert not [k for k in _keys(response.execution) if "sufficien" in k.lower()]
        assert not [
            k for k in _keys(response.manifest or {}) if "sufficien" in k.lower()
        ]


# --- CST-8 implementation choice -------------------------------------------------------------


def test_cst_8_cheaper_equivalent_implementation_chosen(client, channel):
    many, loop = select_stage.CONTEXT_MANY, select_stage.CONTEXT_LOOP
    rows = [
        dict(operator=many.name, version="1", p95_ms=20, quality=dict(f1=1.0)),
        dict(operator=loop.name, version="1", p95_ms=5, quality=dict(f1=1.0)),
    ]
    assert operators.choose("context.fetch", rows) is loop
    worse = [rows[0], dict(rows[1], quality=dict(f1=0.9))]
    assert operators.choose("context.fetch", worse) is many
    missing = [rows[0], dict(rows[1], quality=None)]
    assert operators.choose("context.fetch", missing) is many
    assert operators.choose("context.fetch", []) is many
    # Through recall: the same records either way.
    _dialogue(client)
    request = dict(query="pears", strategy="keyword", k=5, budgets=dict(tokens=500))
    reference = run(**request)
    assert reference.execution["context_fetch"] == "context_many"
    profile(many.name, many.implementation, 20, dict(f1=1.0))
    profile(loop.name, loop.implementation, 5, dict(f1=1.0))
    cheaper = run(**request)
    assert cheaper.execution["context_fetch"] == "context_loop"
    assert [(r["record_id"], r["role"]) for r in cheaper.records] == [
        (r["record_id"], r["role"]) for r in reference.records
    ]
    profile(loop.name, loop.implementation, 5, dict(f1=0.5))
    assert run(**request).execution["context_fetch"] == "context_many"


class ApproximateLexical(Leg):
    name = "fake_lexical_approx"
    exact = False
    implements = "lexical_search"
    implementation = "fake_trigram"

    def run(self, ctx):
        return []


def test_cst_8_no_approximate_substitution_without_opt_in(client, channel, extra):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    extra(ApproximateLexical())
    profile("leg.lexical", "postgres_fts", 100, dict(recall=0.9))
    profile("leg.fake_lexical_approx", "fake_trigram", 1, dict(recall=0.95))
    plain = run(query="orchard", k=2)
    assert "lexical" in stage(plain, "planner")[0]["legs"]
    assert "fake_lexical_approx" not in stage(plain, "planner")[0]["legs"]
    assert "implementation_choices" not in plain.execution
    opted = run(query="orchard", k=2, hints=dict(allow_approximate=True))
    assert opted.execution["implementation_choices"] == dict(
        lexical="fake_lexical_approx"
    )
    assert "lexical" not in stage(opted, "planner")[0]["legs"]
    # The context fetch has no approximate implementation to swap in.
    assert all(s.exact for s in operators.equivalents("context.fetch", False))


# --- determinism, legacy ordering, profiles ---------------------------------------------------


def test_selection_deterministic(client, channel):
    def pool():
        return [
            fake(f"m:{i}", 1.0 if i % 2 else 0.5, f"orchard {i}", thread=i % 3)
            for i in range(1, 12)
        ]

    first = selection.select(pool(), 100, 5, query="orchard 3 4")
    second = selection.select(pool(), 100, 5, query="orchard 3 4")
    assert [c.record_id for c in first[0]] == [c.record_id for c in second[0]]
    assert first[1] == second[1]
    texts = [f"orchard entry {i} {LONG}" for i in range(8)]
    assert (
        ingest(
            client,
            [
                message(i * 100, content=t, thread={"external_id": f"fake-{i % 2}"})
                for i, t in enumerate(texts)
            ],
        ).status_code
        == 200
    )
    a = run(query="orchard entry", k=5, budgets=dict(tokens=150))
    b = run(query="orchard entry", k=5, budgets=dict(tokens=150))
    assert [r["record_id"] for r in a.records] == [r["record_id"] for r in b.records]
    assert a.dropped == b.dropped


def test_existing_search_ordering_untouched(client, channel):
    texts = [
        "orchard apples ladder",
        "orchard path at dawn",
        "ocean and orchard",
        "the orchard gate",
        "orchard orchard harvest",
    ]
    assert (
        ingest(
            client,
            [
                message(i * 4000, content=t, thread={"external_id": f"fake-{i}"})
                for i, t in enumerate(texts)
            ],
        ).status_code
        == 200
    )
    legacy = query("SELECT message_id FROM api.search_records('orchard', 20)", 100)[
        "rows"
    ]
    response = run(query="orchard", strategy="keyword", k=20)
    assert message_ids(response) == [r["message_id"] for r in legacy]
    assert not stage(response, "select") and response.manifest is None
    assert stage(response, "dedup")[0]["status"] == "ok"
    old = query("SELECT id FROM api.search('orchard', 20)", 100)["rows"]
    assert {r["id"] for r in old} == set(message_ids(response))


def test_profiles_written_rate_limited(client, channel, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_RUNTIME_PROFILES", "1")
    planner_module.PLANNER.reset_profile_clock()
    _dialogue(client)
    request = dict(query="pears", strategy="keyword", k=5, budgets=dict(tokens=500))
    run(**request)
    run(**request)

    def counted():
        rows = owner(
            "SELECT operator, implementation, count(*) AS n FROM tl.operator_profiles WHERE source='runtime' GROUP BY 1,2"
        )
        return {(r["operator"], r["implementation"]): r["n"] for r in rows}

    assert counted() == {
        ("leg.lexical", "postgres_fts"): 1,
        ("context.fetch.context_many", "context_many"): 1,
    }
    # A minute later (and in another process) the next measurement is written.
    owner(
        "UPDATE tl.operator_profiles SET measured_at = measured_at - interval '2 minutes'"
    )
    planner_module.PLANNER.reset_profile_clock()
    run(**request)
    assert counted()[("leg.lexical", "postgres_fts")] == 2
    row = owner(
        "SELECT p50_ms, p95_ms FROM tl.operator_profiles WHERE operator='leg.lexical' ORDER BY measured_at DESC LIMIT 1"
    )[0]
    assert row["p95_ms"] >= row["p50_ms"] >= 0


# --- alias pairs -----------------------------------------------------------------------------


def test_p8_same_as_pair_collapsed_keeps_higher_identity_quality(
    client, channel, extra
):
    r = client.post(
        "/v1/channels",
        json=dict(
            name="fake.aliases",
            plugin="synthetic",
            fields=[
                dict(name="identity_quality", type="text", description="Synthetic")
            ],
        ),
    )
    assert r.status_code == 200, r.text

    def aliased(i, quality, thread):
        return message(
            i,
            channel="fake.aliases",
            content=f"orchard meeting notes {i}",
            thread={"external_id": thread},
            fields={"identity_quality": quality},
        )

    assert (
        ingest(
            client,
            [
                aliased(0, "fingerprint", "fake-a"),
                aliased(1, "native_id", "fake-b"),
                aliased(2, "unknown", "fake-c"),
                aliased(3, "unknown", "fake-d"),
                aliased(4, "bridge_id", "fake-e"),
            ],
        ).status_code
        == 200
    )
    weak, strong, tie_a, tie_b, alone = ids(
        "fake.aliases", [f"fake-{i}" for i in range(5)]
    )
    before = run(query="orchard meeting", strategy="keyword", k=10)
    assert len(before.records) == 5 and before.dropped == []
    link("same_as", weak, strong)
    link("same_as", tie_b, tie_a)
    seen = {}

    class Capture(Stage):
        name = "fake_capture"
        order = 35

        def run(self, ctx, candidates):
            seen.update({c.record_id: dict(c.meta) for c in candidates})
            return candidates

    extra.stage(Capture())
    after = run(query="orchard meeting", strategy="keyword", k=10)
    kept = sorted(message_ids(after))
    assert kept == sorted([strong, min(tie_a, tie_b), alone])
    dropped = {d["record_id"]: d for d in after.dropped}
    assert dropped[f"m:{weak}"]["reason"] == "duplicate_alias"
    assert dropped[f"m:{weak}"]["kept"] == f"m:{strong}"
    assert dropped[f"m:{max(tie_a, tie_b)}"]["reason"] == "duplicate_alias"
    assert seen[f"m:{strong}"]["aliases"] == [f"m:{weak}"]
    assert seen[f"m:{min(tie_a, tie_b)}"]["aliases"] == [f"m:{max(tie_a, tie_b)}"]
    assert "aliases" not in seen[f"m:{alone}"]


# --- scenario newest_relevant -----------------------------------------------------------------


def test_newest_relevant_order(client, channel):
    base = AT
    assert (
        ingest(
            client,
            [
                message(0, content=f"orchard plan one {LONG}"),
                message(100, content=f"orchard plan two {LONG}"),
                message(200, content=f"orchard plan three {LONG}"),
                message(300, content="lol ok see you"),
            ],
        ).status_code
        == 200
    )
    one, two, three, chatter = ids(
        "synthetic.chat", ["fake-0", "fake-100", "fake-200", "fake-300"]
    )
    request = dict(
        query="orchard plan",
        legs=["lexical", "structured"],
        k=10,
        expand=False,
        hints=dict(order="newest"),
    )
    response = run(**request)
    assert message_ids(response)[0] == three
    assert message_ids(response) == [three, two, one, chatter]
    assert response.execution["order"] == "newest_relevant"
    first = response.records[0]
    assert first["record_time"] == base + timedelta(seconds=200)
    # Newest absolute is the chatter: only the structured leg found it.
    chatter_record = next(r for r in response.records if r["message_id"] == chatter)
    assert {d["leg"] for d in chatter_record["discovery"]} == {"structured"}
    # Without a content query every hit is ordered by time.
    listed = run(legs=["structured"], k=10, hints=dict(order="newest"), expand=False)
    assert message_ids(listed)[0] == chatter
    # The default order is unchanged.
    plain = run(query="orchard plan", legs=["lexical", "structured"], k=10)
    assert "order" not in plain.execution
