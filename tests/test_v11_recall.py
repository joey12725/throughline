"""Shared retrieval library: recall over every record kind (migration 0038).

Synthetic data only. Embeddings use the deterministic local provider.
"""

import importlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message

from throughline import embeddings, providers
from throughline.db import connect, create_token
from throughline.retrieval import RECORD_KEYS, RecallRequest, recall
from throughline.retrieval import default_planner, hydrate
from throughline.retrieval import legs as legs_module
from throughline.retrieval import stages as stages_module
from throughline.retrieval.legs import Leg
from throughline.retrieval.stages import Stage
from throughline.retrieval.types import RECORD_CONTRACT, Candidate
from throughline.scope import Scope

recall_module = importlib.import_module("throughline.retrieval.recall")

OWNER = Scope.local()
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    """No provider configuration leaks in from the machine running the tests."""
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
            )
        ):
            monkeypatch.delenv(name)


def run(who=OWNER, **kwargs):
    return recall(who, RecallRequest(**kwargs))


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text
    return r.json()


def ids(client_or_none, channel, external_ids=None):
    rows = owner(
        "SELECT m.id, m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id",
        (channel,),
    )
    found = {r["external_id"]: r["id"] for r in rows}
    if external_ids is None:
        return found
    return [found[e] for e in external_ids]


def message_ids(response):
    return [
        r["message_id"] for r in response.records if r["record_id"].startswith("m:")
    ]


def stage(response, name):
    return [s for s in response.execution["stages"] if s["name"] == name]


def embed_all(monkeypatch, dimensions="64"):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", dimensions)
    while embeddings.run_once():
        pass


def person(handle):
    return owner("INSERT INTO tl.people(handle) VALUES (%s) RETURNING id", (handle,))[
        0
    ]["id"]


def restricted(asker="guest"):
    return Scope(
        token_id=None,
        token_kind="agent",
        asker=asker,
        mode="asker",
        unknown_asker="deny",
    )


def grant(asker, channel):
    owner("INSERT INTO tl.askers(name) VALUES (%s) ON CONFLICT DO NOTHING", (asker,))
    owner(
        "INSERT INTO tl.asker_grants(asker,channel_id) SELECT %s,id FROM tl.channels WHERE name=%s",
        (asker, channel),
    )


# --- QRY-13 one interface ------------------------------------------------------


def test_qry_13_one_interface_across_kinds(client, channel):
    register(client, "fake.reference", is_reference=True)
    register(client, "fake.page", is_reference=True, record_kind="page_revision")
    register(client, "fake.derived", record_kind="derived")
    assert (
        ingest(
            client,
            [
                message(0, content="orchard harvest notes"),
                message(
                    1, channel="fake.reference", fields={}, content="orchard manual"
                ),
                message(2, channel="fake.page", fields={}, content="orchard page"),
                message(3, channel="fake.derived", fields={}, content="orchard visit"),
            ],
        ).status_code
        == 200
    )
    default = run(query="orchard", k=10)
    kinds = {r["kind"] for r in default.records}
    # Reference text is opt-in; pages never come from message legs.
    assert kinds == {"life_event", "derived"}
    wider = run(query="orchard", k=10, scope=dict(include_reference=True))
    assert {r["kind"] for r in wider.records} == {"life_event", "derived", "reference"}
    listed = run(
        query="orchard", k=10, scope=dict(kinds=["reference", "page_revision"])
    )
    assert {r["kind"] for r in listed.records} == {"reference"}
    for response in (default, wider, listed):
        assert all(list(r) == list(RECORD_KEYS) for r in response.records)
        assert response.execution["completeness"] == "ranked_sample"


def link(
    kind,
    from_kind,
    from_id,
    to_kind,
    to_id,
    evidence=None,
    method="source",
    confidence=1.0,
):
    from psycopg.types.json import Jsonb

    return owner(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence,method,confidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            kind,
            from_kind,
            from_id,
            to_kind,
            to_id,
            Jsonb(evidence or {}),
            method,
            confidence,
        ),
    )[0]["id"]


def entity(kind, name, metadata=None):
    from psycopg.types.json import Jsonb

    return owner(
        "INSERT INTO tl.entities(kind,name,metadata) VALUES (%s,%s,%s) RETURNING id",
        (kind, name, Jsonb(metadata or {})),
    )[0]["id"]


def clock():
    return owner("SELECT clock_timestamp() AS t")[0]["t"]


def all_stage_ids(response):
    seen = set()
    for value in response.execution["stage_candidates"].values():
        for item in value.values() if isinstance(value, dict) else [value]:
            seen |= set(item)
    return seen


def test_qry_13_entity_and_link_records_returned(client, channel):
    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    m = ids(client, "synthetic.chat", [f"fake-{i}" for i in range(5)])
    farm = entity("concept", "Synthetic Willow Farm")
    link("about", "message", m[1], "entity", farm, evidence={"message_ids": [m[3]]})
    link("about", "entity", farm, "message", m[2])
    reply = link("reply_to", "message", m[0], "message", m[4])

    found = run(query="Synthetic Willow Farm", k=10)
    by_id = {r["record_id"]: r for r in found.records}
    record = by_id[f"e:{farm}"]
    assert record["kind"] == "entity"
    assert record["content"] == "Synthetic Willow Farm (concept)"
    assert record["support"] == [dict(message_id=m[3], version=1, relation="evidence")]
    # Linked messages are hits reached through the entity.
    for message_id in (m[1], m[2]):
        hit = by_id[f"m:{message_id}"]
        assert hit["role"] == "hit"
        assert [
            d["detail"]["via"] for d in hit["discovery"] if d["leg"] == "entity"
        ] == [f"e:{farm}"]
    assert f"m:{m[3]}" not in by_id  # evidence is support, not a hit

    links = run(legs=["links"], scope=dict(message_ids=[m[0], m[4]]), k=10)
    by_id = {r["record_id"]: r for r in links.records}
    assert by_id[f"l:{reply}"]["kind"] == "link"
    assert by_id[f"l:{reply}"]["support"] == [
        dict(message_id=m[0], version=1, relation="from"),
        dict(message_id=m[4], version=1, relation="to"),
    ]
    assert by_id[f"l:{reply}"]["generated"] is False
    assert {f"m:{m[0]}", f"m:{m[4]}"} <= set(by_id)
    for response in (found, links):
        assert all(list(r) == list(RECORD_KEYS) for r in response.records)


# --- MSG-7 record contract ---------------------------------------------------


def test_msg_7_record_contract_keys_identical_for_every_kind(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    m = ids(client, "synthetic.chat", ["fake-0", "fake-1"])
    grove = entity("place", "Synthetic Cedar Grove", {"lat": 1.0, "lon": 2.0})
    link("about", "message", m[0], "entity", grove, evidence={"message_ids": [m[1]]})
    link("reply_to", "message", m[0], "message", m[1])
    records = run(query="Synthetic Cedar Grove", k=20).records
    records += run(query="orchard", k=20).records
    records += run(legs=["links"], scope=dict(message_ids=m), k=20).records
    kinds = {r["kind"] for r in records}
    assert {"life_event", "entity", "link"} <= kinds
    assert {tuple(r) for r in records} == {tuple(RECORD_KEYS)}
    # Every registered record view has exactly the contract columns, typed alike.
    with connect() as c:
        sources = c.execute("SELECT prefix,relation FROM tl.record_sources").fetchall()
        shapes = {
            s["prefix"]: c.execute(
                "SELECT array_agg(a.attname::text ORDER BY a.attnum) n, array_agg(format_type(a.atttypid,a.atttypmod) ORDER BY a.attnum) t FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped",
                (s["relation"],),
            ).fetchone()
            for s in sources
        }
    assert {"m", "l", "e"} <= set(shapes)
    assert all(shape["n"] == list(RECORD_CONTRACT) for shape in shapes.values())
    assert len({tuple(shape["t"]) for shape in shapes.values()}) == 1


def test_page_and_derived_records_flagged_generated_with_provenance_support(
    client, channel
):
    register(client, "fake.page", is_reference=True, record_kind="page_revision")
    register(client, "fake.derived", record_kind="derived")
    assert (
        ingest(
            client,
            [
                message(0, content="orchard source"),
                message(
                    1, channel="fake.derived", fields={}, content="orchard derived"
                ),
                message(2, channel="fake.page", fields={}, content="orchard page text"),
            ],
        ).status_code
        == 200
    )
    response = run(
        query="orchard",
        k=10,
        scope=dict(kinds=["life_event", "derived", "page_revision"]),
    )
    by_kind = {r["kind"]: r for r in response.records}
    # Message legs never return page revisions (pages come from passages).
    assert "page_revision" not in by_kind
    raw, derived = by_kind["life_event"], by_kind["derived"]
    assert raw["generated"] is False and raw["unsupported"] is False
    assert raw["support"] == [
        dict(message_id=raw["message_id"], version=1, relation="self")
    ]
    # A derived record without provenance is generated and unsupported.
    assert derived["generated"] is True and derived["unsupported"] is True
    assert derived["support"] == []
    assert derived["provenance"]["channel"] == "fake.derived"
    page = run(query="page", k=10, scope=dict(kinds=["page_revision"]))
    assert message_ids(page) == []


# --- QRY-20 scope before ranking ----------------------------------------------

WINDOW = range(4990, 5101)
ELIGIBLE = list(range(5000, 5010))


def _shape(i):
    """(channel, person, near) of synthetic message i."""
    if i in ELIGIBLE:
        return "synthetic.chat", "p", True
    if i in WINDOW:
        return (
            ("fake.other", "p", True),
            ("synthetic.chat", "p", False),
            ("synthetic.chat", "q", True),
        )[i % 3]
    if i % 2 == 0:
        return "synthetic.chat", "p", True
    return "synthetic.chat", "q", False


@pytest.fixture()
def ten_thousand(client, channel, monkeypatch):
    register(client, "fake.other")
    batch = []
    for i in range(10000):
        name, _, near = _shape(i)
        location = (
            {"lat": 10.0, "lon": 20.0 + 0.001 * (i % 5)}
            if near
            else {"lat": 40.0, "lon": 40.0}
        )
        batch.append(
            message(
                i,
                channel=name,
                fields={} if name == "fake.other" else {"rating": i},
                content="orchard apple"
                if i in ELIGIBLE
                else "orchard orchard orchard apple apple",
                location=location,
            )
        )
    for start in range(0, len(batch), 5000):
        r = ingest(client, batch[start : start + 5000])
        assert r.status_code == 200, r.text
    p, q = person("synthetic-p"), person("synthetic-q")
    for who, value in (("p", p), ("q", q)):
        owner(
            "UPDATE tl.messages SET person_id=%s WHERE external_id = ANY(%s)",
            (value, [f"fake-{i}" for i in range(10000) if _shape(i)[1] == who]),
        )
    entity("place", "Synthetic Grove", {"lat": 10.0, "lon": 20.0})
    from throughline.derived import materialize

    materialize(wait=True)
    embed_all(monkeypatch)
    found = {}
    for name in ("synthetic.chat", "fake.other"):
        found.update(ids(client, name))
    return dict(p=p, q=q, ids=found)


def _in_scope(message_ids, p):
    rows = owner(
        "SELECT m.id,c.name,m.person_id,m.happened_at,earth_distance(ll_to_earth(m.lat,m.lon),ll_to_earth(10,20)) d FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE m.id = ANY(%s)",
        (list(message_ids),),
    )
    low = AT + timedelta(seconds=WINDOW[0])
    high = AT + timedelta(seconds=WINDOW[-1])
    return len(rows) == len(set(message_ids)) and all(
        r["name"] == "synthetic.chat"
        and r["person_id"] == p
        and low <= r["happened_at"] <= high
        and r["d"] <= 1000
        for r in rows
    )


def test_qry_20_scope_before_ranking_every_leg(ten_thousand):
    data = ten_thousand
    assert owner("SELECT count(*) n FROM tl.messages")[0]["n"] == 10000
    eligible = {data["ids"][f"fake-{i}"] for i in ELIGIBLE}
    scope = dict(
        persons=[data["p"]],
        channels=["synthetic.chat"],
        after=(AT + timedelta(seconds=WINDOW[0])).isoformat(),
        before=(AT + timedelta(seconds=WINDOW[-1])).isoformat(),
        place=dict(name="Synthetic Grove", meters=1000),
    )
    # Every other message ranks higher, so filtering after a global top-k
    # would return nothing.
    for strategy in ("keyword", "fused"):
        response = run(query="orchard apple", k=10, strategy=strategy, scope=scope)
        assert set(message_ids(response)) == eligible, strategy
    meaning = run(query="orchard apple", k=10, strategy="meaning", scope=scope)
    assert stage(meaning, "vector")[0]["status"] == "ok"
    assert message_ids(meaning) and set(message_ids(meaning)) <= eligible
    listed = run(query="", k=10, scope=scope)
    assert stage(listed, "structured")[0]["status"] == "ok"
    assert set(message_ids(listed)) == eligible
    assert _in_scope(message_ids(listed), data["p"])

    # Entity expansion: 3 linked messages in scope among 60.
    farm = entity("concept", "Synthetic Orchard Farm")
    ordered = sorted(eligible)
    targets = ordered[:3] + [data["ids"][f"fake-{i}"] for i in range(4000, 4057)]
    for target in targets:
        link("about", "message", target, "entity", farm)
    via_entity = run(query="Synthetic Orchard Farm", legs=["entity"], k=10, scope=scope)
    assert set(message_ids(via_entity)) == set(ordered[:3])
    assert f"e:{farm}" in {r["record_id"] for r in via_entity.records}

    # Link expansion from a starting set of eligible and ineligible messages.
    decoys = [data["ids"][f"fake-{i}"] for i in range(4000, 4020)]
    for a, b in zip(ordered, ordered[1:]):
        link("next", "message", a, "message", b)
    for a, b in zip(ordered, decoys):
        link("mentions", "message", a, "message", b)
    via_links = run(
        legs=["links"], k=100, scope=dict(scope, message_ids=ordered + decoys)
    )
    assert set(message_ids(via_links)) == eligible
    # 9 chain links, 10 mentions, and the entity links of the 3 eligible
    # seeds: a seed outside the hard scope is never expanded.
    assert sum(r["kind"] == "link" for r in via_links.records) == 9 + 10 + 3
    for response in (meaning, via_entity, via_links):
        hits = [
            r["message_id"]
            for r in response.records
            if r["role"] == "hit" and r["message_id"]
        ]
        assert _in_scope(hits, data["p"])


# --- QRY-18 hard and soft constraints -------------------------------------------


def test_qry_18_hard_scope_and_soft_hints(client, channel):
    register(client, "fake.other")
    assert (
        ingest(
            client,
            [message(0), message(1), message(2, channel="fake.other", fields={})],
        ).status_code
        == 200
    )
    everything = run(query="orchard", k=10)
    assert {r["channel"] for r in everything.records} == {
        "synthetic.chat",
        "fake.other",
    }
    # Hints are soft: they never narrow the result.
    hinted = run(
        query="orchard", k=10, hints={"channels": ["fake.other"], "prefer": "recent"}
    )
    assert {r["record_id"] for r in hinted.records} == {
        r["record_id"] for r in everything.records
    }
    # The hard scope binds, and hints cannot widen it.
    hard = run(
        query="orchard",
        k=10,
        scope=dict(channels=["fake.other"]),
        hints={"channels": ["synthetic.chat"], "relax": True},
    )
    assert [r["channel"] for r in hard.records] == ["fake.other"]
    # Must terms are binding as well.
    assert ingest(client, [message(3, content="orchard ladder")]).status_code == 200
    must = run(query="orchard", k=10, scope=dict(must_terms=["ladder"]))
    assert [r["content"] for r in must.records] == ["orchard ladder"]


def test_qry_18_person_role_involved(client, channel):
    p, q = person("synthetic-p"), person("synthetic-q")
    assert (
        ingest(
            client,
            [
                message(0, thread={"external_id": "fake-a"}),
                message(1, thread={"external_id": "fake-a"}),
                message(2, thread={"external_id": "fake-b"}),
                message(3, thread={"external_id": "fake-c"}),
            ],
        ).status_code
        == 200
    )
    m = ids(client, "synthetic.chat", [f"fake-{i}" for i in range(4)])
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (p, m[0]))
    owner("UPDATE tl.messages SET person_id=%s WHERE id = ANY(%s)", (q, m[1:]))
    link("participant", "message", m[3], "person", p)
    author = run(query="orchard", k=10, scope=dict(persons=[p]))
    assert message_ids(author) == [m[0]]
    involved = run(
        query="orchard", k=10, scope=dict(persons=[p], person_role="involved")
    )
    # The author's own message, a message in the same thread, a linked one.
    assert sorted(message_ids(involved)) == sorted([m[0], m[1], m[3]])
    listed = run(k=10, scope=dict(persons=[p], person_role="involved"))
    assert sorted(message_ids(listed)) == sorted([m[0], m[1], m[3]])


def test_qry_18_unresolvable_place_or_person_returns_empty_with_reason(client, channel):
    assert (
        ingest(client, [message(0, location={"lat": 1, "lon": 2})]).status_code == 200
    )
    cases = (
        (dict(place=dict(name="Synthetic Nowhere")), {}, "place"),
        (dict(persons=[987654]), {}, "persons"),
        ({}, dict(starting_sets=["rs_" + "0" * 32]), "starting_sets"),
    )
    for scope, extra, field in cases:
        response = run(query="orchard", k=10, scope=scope, **extra)
        assert response.records == []
        assert f"unresolvable_scope:{field}" in response.execution["warnings"]
        assert not [s for s in response.execution["stages"] if s.get("kind") == "leg"]
    # A place that resolves scopes the search instead.
    entity("place", "Synthetic Spot", {"lat": 1.0, "lon": 2.0})
    entity("place", "Synthetic Far", {"lat": 50.0, "lon": 50.0})
    near = run(query="orchard", k=10, scope=dict(place=dict(name="Synthetic Spot")))
    assert len(near.records) == 1
    far = run(query="orchard", k=10, scope=dict(place=dict(name="Synthetic Far")))
    # The only warning is the coverage notice that an empty result over
    # sources not reported complete is not evidence of absence (source coverage).
    assert far.records == []
    assert not [w for w in far.execution["warnings"] if w != "absence_is_not_evidence"]


# --- MSG-9 time basis -------------------------------------------------------------


def test_msg_9_time_basis_subject_versus_record(client, channel):
    assert (
        ingest(
            client,
            [
                message(
                    0,
                    happened_at="2020-01-01T00:00:00Z",
                    content="orchard trip notes",
                    subject=dict(
                        start="2019-06-01T00:00:00Z",
                        end="2019-08-31T00:00:00Z",
                        start_precision="day",
                        end_precision="day",
                    ),
                ),
                message(
                    1, happened_at="2019-07-01T12:00:00Z", content="orchard trip day"
                ),
                message(
                    2,
                    happened_at="2021-01-01T00:00:00Z",
                    content="orchard open ended",
                    subject=dict(start="2019-07-15T00:00:00Z"),
                ),
            ],
        ).status_code
        == 200
    )
    m = ids(client, "synthetic.chat", ["fake-0", "fake-1", "fake-2"])
    july = dict(after="2019-07-01T00:00:00Z", before="2019-07-31T00:00:00Z")
    january = dict(after="2020-01-01T00:00:00Z", before="2020-01-02T00:00:00Z")

    def found(window, basis):
        response = run(query="orchard", k=10, scope=dict(window, time_basis=basis))
        return sorted(message_ids(response))

    assert found(july, "record") == [m[1]]
    # The subject interval; no interval falls back to record time; an open
    # subject end is unbounded.
    assert found(july, "subject") == sorted([m[0], m[1], m[2]])
    assert found(january, "record") == [m[0]]
    assert found(january, "subject") == [m[2]]
    assert found(january, "either") == sorted([m[0], m[2]])


# --- exclusions ------------------------------------------------------------------


def test_exclude_seen_is_applied_before_limit(client, channel):
    assert ingest(client, [message(i) for i in range(30)]).status_code == 200
    first = run(query="orchard", k=10, strategy="keyword")
    assert len(first.records) == 10
    seen = [r["record_id"] for r in first.records]
    second = run(query="orchard", k=10, strategy="keyword", exclude_seen=seen)
    assert len(second.records) == 10
    assert not set(seen) & {r["record_id"] for r in second.records}
    third = run(
        query="orchard",
        k=10,
        strategy="keyword",
        exclude_seen=[r["message_id"] for r in first.records + second.records],
    )
    assert len(third.records) == 10
    listed = run(k=25, exclude_seen=seen)
    assert len(listed.records) == 20


# --- QRY-14 concurrency ------------------------------------------------------------


def test_qry_14_legs_concurrent_separate_connections(client, channel, monkeypatch):
    from contextlib import contextmanager

    from throughline import query as query_module

    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    real = query_module.reader_transaction
    lock = threading.Lock()
    state = dict(active=0, peak=0, pids=set())

    @contextmanager
    def tracked(scope, deadline_ms=None):
        with real(scope, deadline_ms) as conn:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["pids"].add(conn.info.backend_pid)
            try:
                time.sleep(0.3)
                yield conn
            finally:
                with lock:
                    state["active"] -= 1

    monkeypatch.setattr(query_module, "reader_transaction", tracked)
    response = run(query="orchard", legs=["lexical", "structured"], k=5)
    legs = {s["name"] for s in response.execution["stages"] if s.get("kind") == "leg"}
    assert legs == {"lexical", "structured"}
    assert state["peak"] >= 2 and len(state["pids"]) >= 2
    # Two 300 ms legs side by side, not one after the other.
    assert response.execution["timings_ms"]["retrieval"] < 550
    monkeypatch.setattr(query_module, "reader_transaction", real)
    # Raw search needs no pages and no embeddings.
    raw = run(query="orchard", raw_only=True, k=5)
    assert len(raw.records) == 5
    ran = stage(raw, "planner")[0]["legs"]
    assert "entity" not in ran and "links" not in ran
    assert stage(raw, "vector")[0]["reason"] == "embeddings_unavailable"
    assert stage(raw, "lexical")[0]["status"] == "ok"


# --- QRY-22 degradation --------------------------------------------------------------


@pytest.fixture()
def extra_legs():
    added = []

    def add(leg):
        legs_module.register(leg)
        added.append((legs_module, leg.name))
        return leg

    def add_stage(stage_object):
        stages_module.register(stage_object)
        added.append((stages_module, stage_object.name))
        return stage_object

    add.stage = add_stage
    yield add
    for module, name in added:
        module.unregister(name)


class SlowLeg(Leg):
    name = "fake_slow"

    def run(self, ctx):
        time.sleep(2)
        return [Candidate(record_id="m:1")]


class BrokenLeg(Leg):
    name = "fake_broken"

    def run(self, ctx):
        raise RuntimeError("synthetic failure")


class BrokenStage(Stage):
    name = "fake_broken_stage"
    order = 25

    def run(self, ctx, candidates):
        raise RuntimeError("synthetic failure")


def test_qry_22_vector_unavailable_leg_timeout_leg_exception_degrade_with_disclosure(
    client, channel, monkeypatch, extra_legs
):
    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    extra_legs(SlowLeg())
    extra_legs(BrokenLeg())
    extra_legs.stage(BrokenStage())
    monkeypatch.setenv("THROUGHLINE_LEG_TIMEOUT_MS", "300")
    started = time.perf_counter()
    response = run(
        query="orchard", legs=["lexical", "vector", "fake_slow", "fake_broken"], k=5
    )
    assert time.perf_counter() - started < 1.8
    execution = response.execution
    assert execution["status"] == "degraded"
    assert len(response.records) == 5
    assert stage(response, "vector")[0]["status"] == "skipped"
    assert stage(response, "vector")[0]["reason"] == "embeddings_unavailable"
    assert stage(response, "fake_slow")[0]["status"] == "timeout"
    assert stage(response, "fake_broken")[0]["status"] == "failed"
    assert stage(response, "fake_broken")[0]["reason"] == "exception:RuntimeError"
    assert stage(response, "fake_broken_stage")[0]["status"] == "failed"
    incomplete = {i["stage"] for i in execution["incomplete"]}
    assert {"vector", "fake_slow", "fake_broken", "fake_broken_stage"} <= incomplete
    # No error text or data reaches the disclosure.
    assert "synthetic failure" not in str(execution)


def test_qry_22_deadline_during_hydration_or_scope_degrades(
    client, channel, monkeypatch
):
    """A deadline that passes while candidates are read (or while names in the
    scope are resolved) degrades the recall with a disclosure, never a 500."""
    from throughline.retrieval import executor, scope_sql

    assert ingest(client, [message(i) for i in range(3)]).status_code == 200

    def expired(*args, **kwargs):
        raise executor.LegTimeout()

    with monkeypatch.context() as patch:
        patch.setattr(hydrate, "records", expired)
        response = run(query="orchard", legs=["lexical"], k=5)
        assert response.records == []
        assert response.execution["status"] == "degraded"
        assert stage(response, "hydrate")[0]["status"] == "timeout"
        assert response.dropped and all(
            d["reason"] == "timeout" for d in response.dropped
        )
        exhaustive = run(query="", exhaustive=True, k=5)
        assert exhaustive.execution["completeness"] == "truncated"
        over_http = client.post("/v1/recall", json={"query": "orchard", "k": 5})
        assert over_http.status_code == 200, over_http.text
        assert over_http.json()["execution"]["status"] == "degraded"
    with monkeypatch.context() as patch:
        patch.setattr(scope_sql, "resolve", expired)
        response = run(query="orchard", k=5)
        assert response.records == []
        assert response.execution["status"] == "degraded"
        assert stage(response, "scope")[0]["reason"] == "timeout"
    # A real 1 ms deadline never fails the call either.
    for _ in range(10):
        tiny = client.post(
            "/v1/recall", json={"query": "orchard", "budgets": {"deadline_ms": 1}}
        )
        assert tiny.status_code == 200, tiny.text
    # Other failures are not hidden as timeouts.
    with monkeypatch.context() as patch:

        def broken(*args, **kwargs):
            raise RuntimeError("synthetic failure")

        patch.setattr(hydrate, "records", broken)
        with pytest.raises(RuntimeError):
            run(query="orchard", legs=["lexical"], k=5)


# --- QRY-29 discovery versus support ------------------------------------------------


def test_qry_29_discovery_per_leg_and_support_separate(client, channel, monkeypatch):
    assert (
        ingest(
            client,
            [
                message(0, content="orchard ladder at dawn"),
                message(4000, content="ocean waves", thread={"external_id": "fake-b"}),
            ],
        ).status_code
        == 200
    )
    embed_all(monkeypatch)
    response = run(query="orchard ladder", strategy="fused", k=5)
    top = response.records[0]
    legs = {d["leg"]: d for d in top["discovery"]}
    assert set(legs) == {"lexical", "vector"}
    assert all(d["rank"] == 1 and d["score"] is not None for d in legs.values())
    assert top["score"] == pytest.approx(2 / 61)
    # Why it was found is not what it may be cited for.
    assert top["support"] == [
        dict(message_id=top["message_id"], version=1, relation="self")
    ]


# --- QRY-19 overrides -----------------------------------------------------------------


def test_qry_19_overrides(client, channel):
    assert ingest(client, [message(i) for i in range(20)]).status_code == 200
    keyword = run(query="orchard", strategy="keyword", k=3)
    assert stage(keyword, "planner")[0]["legs"] == ["lexical"]
    assert len(keyword.records) == 3
    chosen = run(query="orchard", legs=["structured"], k=4)
    assert stage(chosen, "planner")[0]["legs"] == ["structured"]
    assert len(chosen.records) == 4
    raw = run(query="orchard", raw_only=True, expand=False, k=20)
    assert set(stage(raw, "planner")[0]["legs"]) <= {"lexical", "vector", "structured"}
    # A token budget cuts the result and reports it.
    small = run(query="orchard", strategy="keyword", k=20, budgets=dict(tokens=20))
    assert 0 < len(small.records) < 20
    assert sum(r["tokens"] for r in small.records) <= 20
    assert small.execution["truncation"]["reason"] == "token_budget"
    assert {d["reason"] for d in small.dropped} == {"budget"}
    # A caller can only lower the token's limits.
    with connect() as c:
        row = c.execute("SELECT * FROM tl.tokens ORDER BY id LIMIT 1").fetchone()
    limited = recall(
        OWNER,
        RecallRequest(query="orchard", budgets=dict(tokens=10**9, deadline_ms=10**9)),
        token_row=row,
    )
    limits = limited.execution["budget"]["limits"]
    assert limits["tokens"] == row["max_return_tokens"]
    assert limits["deadline_ms"] == row["max_deadline_ms"]


# --- QRY-30 / QRY-38 ----------------------------------------------------------------


def test_qry_30_exhaustive_vs_sample(client, channel, monkeypatch):
    assert ingest(client, [message(i) for i in range(50)]).status_code == 200
    sample = run(query="orchard", k=10)
    assert len(sample.records) == 10
    assert sample.execution["completeness"] == "ranked_sample"
    everything = run(query="orchard", exhaustive=True)
    assert len(everything.records) == 50
    assert everything.execution["completeness"] == "exhaustive"
    assert stage(everything, "planner")[0]["legs"] == ["lexical"]
    listed = run(exhaustive=True)
    assert listed.execution["completeness"] == "exhaustive"
    assert [r["external_id"] for r in listed.records] == [
        f"fake-{i}" for i in range(50)
    ]
    monkeypatch.setenv("THROUGHLINE_RECALL_EXHAUSTIVE_CAP", "20")
    capped = run(query="orchard", exhaustive=True)
    assert len(capped.records) == 20
    assert capped.execution["completeness"] == "truncated"
    assert capped.execution["truncation"]["reason"] == "row_cap"
    monkeypatch.delenv("THROUGHLINE_RECALL_EXHAUSTIVE_CAP")
    budget = run(exhaustive=True, budgets=dict(tokens=30))
    assert budget.execution["completeness"] == "truncated"
    assert budget.execution["truncation"]["reason"] == "token_budget"


class ApproximateStage(Stage):
    name = "fake_approximate"
    order = 60
    exact = False


def test_qry_38_stage_exactness_flags(client, channel, monkeypatch, extra_legs):
    assert (
        ingest(
            client,
            [
                message(0),
                message(4000, content="ocean", thread={"external_id": "fake-b"}),
            ],
        ).status_code
        == 200
    )
    exact = run(query="orchard", strategy="keyword", exhaustive=True)
    assert stage(exact, "lexical")[0]["exact"] is True
    assert exact.execution["exactness"] == "exact"
    assert exact.execution["completeness"] == "exhaustive"
    embed_all(monkeypatch)
    fused = run(query="orchard", strategy="fused", k=5)
    assert stage(fused, "vector")[0]["exact"] is False
    assert fused.execution["exactness"] == "mixed"
    assert fused.execution["completeness"] == "ranked_sample"
    extra_legs.stage(ApproximateStage())
    staged = run(query="orchard", strategy="keyword", k=5)
    assert stage(staged, "fake_approximate")[0]["exact"] is False
    assert staged.execution["exactness"] == "mixed"
    # The catalogue marks ranked functions and their limit argument.
    names = ["search_records", "vector_records", "entity_search", "list_records"]
    rows = {
        r["name"]: r
        for r in owner(
            "SELECT name,exactness,ranked_limit_arg FROM tl.api_functions WHERE name = ANY(%s)",
            (names,),
        )
    }
    assert rows["search_records"]["ranked_limit_arg"] == 2
    assert rows["vector_records"]["exactness"] == "approximate"
    assert rows["entity_search"]["ranked_limit_arg"] == 2
    assert rows["list_records"]["ranked_limit_arg"] is None


# --- QRY-17 fusion -------------------------------------------------------------------


def test_qry_17_rrf_matches_search_fused_order(client, channel, monkeypatch):
    texts = [
        "orchard apples ladder",
        "orchard path at dawn",
        "ocean and orchard",
        "apples in a basket",
        "ladder against the wall",
        "the orchard gate",
        "quiet morning walk",
        "orchard orchard harvest",
    ]
    assert (
        ingest(
            client,
            [
                message(i * 4000, content=text, thread={"external_id": f"fake-{i}"})
                for i, text in enumerate(texts)
            ],
        ).status_code
        == 200
    )
    embed_all(monkeypatch)
    vector, model = embeddings.embed_search("orchard apples", with_model=True)
    from throughline.query import query

    legacy = query(
        "SELECT id, score FROM api.search_fused('orchard apples','"
        + embeddings.vector(vector)
        + "',20,NULL,NULL,NULL,NULL,NULL,'"
        + model
        + "')"
    )["rows"]
    response = run(query="orchard apples", strategy="fused", k=20)
    assert len(legacy) > 3
    assert message_ids(response) == [r["id"] for r in legacy]
    for record, row in zip(response.records, legacy):
        assert record["score"] == pytest.approx(row["score"], rel=1e-5)
    # The constant can be changed per request.
    sharper = run(query="orchard apples", strategy="fused", k=20, rrf_k=1)
    assert sharper.records[0]["score"] > response.records[0]["score"]


# --- QRY-15 / QRY-21 planner protocol ---------------------------------------------------


class StubPlanner:
    name = "stub"

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []

    def initial(self, ctx):
        self.calls.append("initial")
        ctx.manifest = {"by": "fake", "groups": []}
        ctx.extras["fake"] = 1
        ctx.after_recall.append(
            lambda c, records: self.calls.append(("callback", len(records)))
        )
        return ["lexical"]

    def next_round(self, ctx, candidates, round_no):
        self.calls.append(("next", round_no, len(candidates)))
        return self.rounds(round_no)

    def after_recall(self, ctx):
        self.calls.append("after")


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_qry_15_qry_21_planner_protocol_loop(client, channel, monkeypatch):
    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    # One round only when next_round names no legs.
    stop = StubPlanner(lambda n: (None, "enough_candidates"))
    monkeypatch.setattr(recall_module, "planner", lambda: stop)
    response = run(query="orchard", k=3)
    planner = stage(response, "planner")[0]
    assert planner["rounds"] == 1 and planner["stop_reason"] == "enough_candidates"
    assert planner["legs"] == ["lexical"] and not stage(response, "structured")
    assert stop.calls == ["initial", ("next", 1, 5), ("callback", 3), "after"]
    assert response.manifest == {"by": "fake", "groups": []}
    assert response.extras == {"fake": 1}
    # A second round runs only when named; rounds are bounded.
    more = StubPlanner(lambda n: (["structured"] if n == 1 else ["lexical"], None))
    monkeypatch.setattr(recall_module, "planner", lambda: more)
    monkeypatch.setenv("THROUGHLINE_RECALL_MAX_ROUNDS", "2")
    response = run(query="orchard", k=3)
    planner = stage(response, "planner")[0]
    assert planner["rounds"] == 2 and planner["stop_reason"] == "max_rounds"
    assert planner["legs"] == ["lexical", "structured"]
    rounds = [
        s["round"] for s in response.execution["stages"] if s.get("kind") == "leg"
    ]
    assert rounds == [1, 2]
    assert [c for c in more.calls if c[0] == "next"] == [
        ("next", 1, 5),
        ("next", 2, 5),
    ]
    # Never a sufficiency flag.
    assert not [k for k in _keys(response.execution) if "sufficien" in k.lower()]
    monkeypatch.undo()
    # Without a planner module the default one-round planner is used.
    active = recall_module.planner()
    assert active is default_planner.PLANNER or active.name != "stub"


# --- QRY-40 ---------------------------------------------------------------------------


def test_qry_40_no_semantic_or_controller_call(client, channel, monkeypatch):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    asked = []
    real_get, real_call = providers.get, providers.call

    def get(kind):
        asked.append(kind)
        return real_get(kind)

    def call(kind, *args, **kwargs):
        asked.append(("call", kind))
        return real_call(kind, *args, **kwargs)

    monkeypatch.setattr(providers, "get", get)
    monkeypatch.setattr(providers, "call", call)
    response = run(query="orchard", k=3, hints={"escalate": True, "librarian": True})
    assert len(response.records) == 3
    forbidden = {"semantic", "controller", ("call", "semantic"), ("call", "controller")}
    assert not forbidden & set(asked)


# --- BRN-5 / BEN-21 as-of ----------------------------------------------------------------


def test_brn_5_as_of_candidate_generation(client, channel):
    assert ingest(client, [message(0, content="orchard lantern")]).status_code == 200
    old = ids(client, "synthetic.chat", ["fake-0"])[0]
    cutoff = clock()
    time.sleep(0.05)
    edited = ingest(client, [message(0, content="orchard meadow")])
    assert edited.json()["new_versions"] == 1
    later = message(
        1, happened_at="2030-01-01T00:00:00Z", content="orchard lantern later"
    )
    assert ingest(client, [later]).status_code == 200
    then = dict(known_by=cutoff.isoformat(), as_of="2025-01-01T00:00:00Z")
    lantern = run(query="lantern", k=5, scope=then)
    assert message_ids(lantern) == [old]
    assert lantern.records[0]["content"] == "orchard lantern"
    assert lantern.records[0]["is_current"] is False
    assert lantern.execution["as_of_honoured"]["lexical"] is True
    assert run(query="meadow", k=5, scope=then).records == []
    # Current state: the edit is the only version, and the later record exists.
    now = run(query="lantern", k=5)
    assert [r["content"] for r in now.records] == ["orchard lantern later"]
    assert [r["content"] for r in run(query="meadow", k=5).records] == [
        "orchard meadow"
    ]
    # as_of alone: a record-time cutoff over current versions.
    alone = run(query="lantern", k=5, scope=dict(as_of="2025-01-01T00:00:00Z"))
    assert alone.records == []


def test_ben_21_no_post_cutoff_id_in_any_stage(client, channel, monkeypatch):
    assert (
        ingest(
            client,
            [message(0, content="orchard alpha"), message(1, content="orchard beta")],
        ).status_code
        == 200
    )
    m0, m1 = ids(client, "synthetic.chat", ["fake-0", "fake-1"])
    farm = entity("concept", "Synthetic Orchard Farm")
    early = link("reply_to", "message", m0, "message", m1)
    link("about", "message", m1, "entity", farm)
    embed_all(monkeypatch)
    cutoff = clock()
    time.sleep(0.05)
    edited = ingest(client, [message(1, content="orchard beta gamma")])
    assert edited.json()["new_versions"] == 1
    late_message = message(
        2, happened_at="2019-12-01T00:00:00Z", content="orchard delta"
    )
    assert ingest(client, [late_message]).status_code == 200
    m1_new, m2 = ids(client, "synthetic.chat", ["fake-1", "fake-2"])
    late = link("reply_to", "message", m0, "message", m2)
    link("about", "message", m2, "entity", farm)
    # An entity first linked after the cutoff did not exist then.
    late_farm = entity("concept", "Synthetic Orchard Farm Annex")
    link("about", "message", m2, "entity", late_farm)
    late_ids = {f"m:{m1_new}", f"m:{m2}", f"l:{late}", f"e:{late_farm}"}

    scope = dict(as_of="2021-01-01T00:00:00Z", known_by=cutoff.isoformat())
    searched = run(query="orchard", k=20, scope=scope, trace=True)
    named = run(
        query="Synthetic Orchard Farm", legs=["entity"], k=20, scope=scope, trace=True
    )
    seeds = [m0, m1, m1_new, m2]
    linked = run(legs=["links"], k=20, scope=dict(scope, message_ids=seeds), trace=True)
    for response in (searched, named, linked):
        seen = {r["record_id"] for r in response.records} | all_stage_ids(response)
        assert not seen & late_ids, seen & late_ids
        assert all(
            r["ingested_at"] is None or r["ingested_at"] <= cutoff
            for r in response.records
        )
    assert {f"m:{m0}", f"m:{m1}"} <= {r["record_id"] for r in searched.records}
    assert f"l:{early}" in {r["record_id"] for r in linked.records}
    vector = stage(searched, "vector")[0]
    assert vector["status"] == "skipped"
    assert vector["reason"] == "embeddings_current_only"
    honoured = searched.execution["as_of_honoured"]
    assert honoured["lexical"] is True and honoured["vector"] is False
    assert f"e:{farm}" in {r["record_id"] for r in named.records}
    assert "entity" in named.execution["as_of_honoured"]
    # Without a cutoff the later entity is found.
    current = run(query="Synthetic Orchard Farm", legs=["entity"], k=20)
    assert f"e:{late_farm}" in {r["record_id"] for r in current.records}
    assert "links" in linked.execution["as_of_honoured"]


# --- ACL-8 -------------------------------------------------------------------------------


def test_acl_8_restricted_asker_recall(client, channel):
    register(client, "fake.hidden")
    assert (
        ingest(
            client,
            [message(i) for i in range(3)]
            + [message(10 + i, channel="fake.hidden", fields={}) for i in range(4)],
        ).status_code
        == 200
    )
    shown = ids(client, "synthetic.chat")
    hidden = ids(client, "fake.hidden")
    farm = entity("concept", "Synthetic Orchard Farm")
    about = link("about", "message", shown["fake-0"], "entity", farm)
    visible = link("reply_to", "message", shown["fake-0"], "message", shown["fake-1"])
    secret = link("reply_to", "message", shown["fake-0"], "message", hidden["fake-10"])
    grant("guest", "synthetic.chat")
    guest = restricted("guest")
    response = run(guest, query="orchard", k=20, trace=True)
    assert {r["channel"] for r in response.records} == {"synthetic.chat"}
    assert len(response.records) == 3
    assert stage(response, "entity")[0]["reason"] == "not_visible_for_asker"
    assert response.execution["counts"]["per_leg"]["lexical"] == 3
    allowed = {f"m:{i}" for i in shown.values()}
    assert all_stage_ids(response) <= allowed
    seeds = list(shown.values()) + list(hidden.values())
    links = run(guest, legs=["links"], k=20, scope=dict(message_ids=seeds))
    found = {r["record_id"] for r in links.records}
    # A link with a hidden message end is hidden; one to an entity is not.
    assert f"l:{visible}" in found and f"l:{secret}" not in found
    assert found <= allowed | {f"l:{visible}", f"l:{about}"}
    # The owner sees everything.
    assert len(message_ids(run(query="orchard", k=20))) == 7
    # An asker without grants sees nothing at all.
    assert run(restricted("nobody"), query="orchard", k=20).records == []


def test_acl_8_agent_token_over_http_is_scoped(client, channel):
    register(client, "fake.hidden")
    assert (
        ingest(
            client, [message(0), message(1, channel="fake.hidden", fields={})]
        ).status_code
        == 200
    )
    grant("guest", "synthetic.chat")
    agent = create_token("synthetic agent", kind="agent", askers=["guest"])["token"]
    r = client.post(
        "/v1/recall",
        json=dict(query="orchard", k=10),
        headers={"Authorization": "Bearer " + agent},
    )
    assert r.status_code == 200, r.text
    assert {x["channel"] for x in r.json()["records"]} == {"synthetic.chat"}
    row = owner(
        "SELECT * FROM tl.audit_log WHERE asker='guest' ORDER BY id DESC LIMIT 1"
    )[0]
    assert row["row_count"] == 1 and "orchard" not in str(row["detail"])


# --- single hydrator ------------------------------------------------------------------


def test_single_hydrator(client, channel):
    assert ingest(client, [message(i) for i in range(4)]).status_code == 200
    m = ids(client, "synthetic.chat", ["fake-0", "fake-1"])
    farm = entity("concept", "Synthetic Orchard Farm")
    link("about", "message", m[0], "entity", farm, evidence={"message_ids": [m[1]]})
    link("reply_to", "message", m[0], "message", m[1])
    records = run(query="orchard Synthetic Orchard Farm", k=20).records
    records += run(legs=["links"], k=20, scope=dict(message_ids=m)).records
    assert {r["kind"] for r in records} >= {"life_event", "entity", "link"}
    keys = ("record_id", "span", "conversation", "score", "discovery", "role")
    members = [{key: r[key] for key in keys} for r in records]
    again = hydrate.records_for_members(OWNER, members)
    assert len(again) == len(records)
    # Support an entity leg collected is candidate state; everything the
    # record views provide is identical.
    for a, b in zip(records, again):
        if a["kind"] == "entity":
            a = {k: v for k, v in a.items() if k not in ("support", "unsupported")}
            b = {k: v for k, v in b.items() if k not in ("support", "unsupported")}
        assert a == b
    # Result-set style members (prefix, ref_id, pinned message id) too.
    pinned = hydrate.records_for_members(
        OWNER,
        [dict(prefix="m", ref_id=m[0], message_id=m[0], span_first=1, span_last=1)],
    )
    assert pinned[0]["record_id"] == f"m:{m[0]}"
    assert pinned[0]["span"] == dict(first_no=1, last_no=1)
    assert list(pinned[0]) == list(RECORD_KEYS)
    # A restricted asker hydrates nothing it may not see.
    assert hydrate.records_for_members(restricted("nobody"), members) == []


# --- CST-9 timings --------------------------------------------------------------------


class TimedStage(Stage):
    name = "fake_timed"
    order = 45


def test_cst_9_stage_timings(client, channel, monkeypatch, extra_legs):
    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    embed_all(monkeypatch)
    extra_legs.stage(TimedStage())
    timings = run(query="orchard", strategy="fused", k=5).execution["timings_ms"]
    for name in (
        "materialize",
        "database",
        "retrieval",
        "leg:lexical",
        "leg:vector",
        "embedding",
        "fusion",
        "planner",
        "stage:fake_timed",
        "hydrate",
        "total",
    ):
        assert name in timings and timings[name] >= 0, name
    assert timings["total"] >= timings["retrieval"]


# --- QRY-16 levels and surfaces --------------------------------------------------------


def test_qry_16_levels_callable(client, channel, monkeypatch):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    first = ids(client, "synthetic.chat", ["fake-0"])[0]
    names = [
        "scope_filter",
        "list_records",
        "search_records",
        "vector_records",
        "entity_search",
        "context_many",
    ]
    levels = {
        r["name"]: r["level"]
        for r in owner(
            "SELECT name,level FROM tl.api_functions WHERE name = ANY(%s)", (names,)
        )
    }
    assert levels == dict(
        scope_filter=0,
        list_records=0,
        search_records=1,
        vector_records=1,
        entity_search=1,
        context_many=2,
    )
    # Exact functions and ranked ones over validated SQL.
    for statement, count in (
        ("SELECT * FROM api.list_records(NULL,ARRAY['synthetic.chat'])", 3),
        (f"SELECT * FROM api.scope_filter(ARRAY[{first}]::bigint[])", 1),
        ("SELECT * FROM api.search_records('orchard',2)", 2),
        (f"SELECT * FROM api.context_many(ARRAY[{first}]::bigint[],0,1)", 2),
    ):
        r = client.post("/v1/sql", json={"statement": statement})
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == count, statement
    # The vector function refuses as_of explicitly.
    refused = client.post(
        "/v1/sql",
        json={
            "statement": "SELECT * FROM api.vector_records('[1,0,0]',5,as_of=>'2020-01-01Z')"
        },
    )
    assert refused.status_code == 400
    # Recall over HTTP.
    r = client.post("/v1/recall", json=dict(query="orchard", k=2, trace=True))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["records"]) == 2
    assert list(body["records"][0]) == list(RECORD_KEYS)
    assert body["execution"]["trace_id"]
    assert client.get(f"/v1/traces/{body['execution']['trace_id']}").status_code == 200
    assert client.post("/v1/recall", json=dict(query="orchard", k=0)).status_code == 422
    assert client.post("/v1/recall", json=dict(query="x", fake=1)).status_code == 422
    describe = client.get("/v1/describe", params={"subject": "recall"}).json()
    assert "guarantees" in describe and "scope_filter" in describe["functions"]
    # The command line.
    import throughline.cli as cli
    from typer.testing import CliRunner

    sent = []

    def request(method, path, data=None, params=None):
        sent.append((method, path, data))
        return client.request(method, "/v1/" + path, json=data, params=params).json()

    monkeypatch.setattr(cli, "request", request)
    result = CliRunner().invoke(
        cli.app, ["lib", "recall", "orchard", "--k", "2", "--channel", "synthetic.chat"]
    )
    assert result.exit_code == 0, result.output
    assert sent[0][:2] == ("POST", "recall")
    assert sent[0][2]["scope"] == {
        "channels": ["synthetic.chat"],
        "person_role": "author",
        "time_basis": "record",
    }
    assert "2 records" in result.output
    result = CliRunner().invoke(cli.app, ["lib", "recall", "orchard", "--json"])
    assert result.exit_code == 0 and '"records"' in result.output


# --- optional feature ---------------------------------------------------------------


def test_wave_integration_save_and_starting_sets(client, channel):
    pytest.importorskip("throughline.resultsets")
    from throughline.query import query_ex

    assert ingest(client, [message(i) for i in range(6)]).status_code == 200
    saved = run(query="orchard", k=3, save=True)
    handle = saved.result_set
    assert handle and handle.startswith("rs_"), saved.execution["incomplete"]
    rows = query_ex(f"SELECT * FROM api.result_set('{handle}')", 100, scope=OWNER)[
        "rows"
    ]
    assert sorted(r["record_id"] for r in rows) == sorted(
        r["record_id"] for r in saved.records
    )
    # A saved set is a starting set: the hard scope is its members.
    within = run(query="orchard", k=10, starting_sets=[handle])
    assert {r["record_id"] for r in within.records} == {
        r["record_id"] for r in saved.records
    }


# --- review follow-ups -------------------------------------------------------------------


def test_non_message_kinds_only_scope(client, channel):
    """kinds naming only entity or link records: the message legs step aside
    and the empty message-kind list keeps its SQL type."""
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    m = ids(client, "synthetic.chat", ["fake-0", "fake-1"])
    farm = entity("concept", "Synthetic Willow Farm")
    link("about", "message", m[0], "entity", farm)
    reply = link("reply_to", "message", m[0], "message", m[1])
    only_entities = run(
        query="Synthetic Willow Farm", k=10, scope=dict(kinds=["entity"])
    )
    assert [r["record_id"] for r in only_entities.records] == [f"e:{farm}"]
    assert stage(only_entities, "entity")[0]["status"] == "ok"
    assert stage(only_entities, "lexical")[0]["reason"] == "kinds_excluded"
    only_links = run(legs=["links"], k=10, scope=dict(kinds=["link"], message_ids=m))
    assert stage(only_links, "links")[0]["status"] == "ok"
    # Message ends are not admitted, so no seed is in scope.
    assert only_links.records == []
    both = run(
        legs=["links"], k=10, scope=dict(kinds=["link", "life_event"], message_ids=m)
    )
    assert f"l:{reply}" in {r["record_id"] for r in both.records}
    # Empty lists are typed: an empty channel list admits nothing, an empty
    # must_terms list constrains nothing.
    assert run(query="orchard", k=10, scope=dict(channels=[])).records == []
    assert len(run(query="orchard", k=10, scope=dict(must_terms=[])).records) == 3


def test_exclude_seen_non_message_records(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    m = ids(client, "synthetic.chat", ["fake-0", "fake-1", "fake-2"])
    first = link("reply_to", "message", m[0], "message", m[1])
    second = link("reply_to", "message", m[1], "message", m[2])
    seen = run(legs=["links"], k=10, scope=dict(message_ids=m))
    assert {f"l:{first}", f"l:{second}"} <= {r["record_id"] for r in seen.records}
    again = run(
        legs=["links"], k=10, scope=dict(message_ids=m), exclude_seen=[f"l:{first}"]
    )
    found = {r["record_id"] for r in again.records}
    assert f"l:{first}" not in found and f"l:{second}" in found


def test_near_and_place_together_rejected(client, channel):
    r = client.post(
        "/v1/recall",
        json=dict(
            query="orchard",
            scope=dict(near=dict(lat=1, lon=2), place=dict(name="Synthetic Spot")),
        ),
    )
    assert r.status_code == 422


def test_involved_never_matches_through_a_hidden_message(client, channel):
    """person_role involved reads thread co-authors through the gated views:
    a message the asker cannot see never makes a visible one match."""
    from throughline.query import query_ex

    p = person("synthetic-p")
    register(client, "fake.hidden")
    register(client, "fake.derived", record_kind="derived")
    thread = {"external_id": "fake-a"}
    assert (
        ingest(
            client,
            [
                message(0, channel="fake.hidden", fields={}),
                message(1, channel="fake.derived", fields={}, thread=thread),
                message(2, channel="fake.derived", fields={}, thread=thread),
            ],
        ).status_code
        == 200
    )
    (source,) = ids(client, "fake.hidden", ["fake-0"])
    d1, d2 = ids(client, "fake.derived", ["fake-1", "fake-2"])
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (p, d2))
    grant("guest", "fake.derived")
    statement = (
        f"SELECT * FROM api.scope_filter(ARRAY[{d1}]::bigint[],"
        f"persons=>ARRAY[{p}]::bigint[],person_role=>'involved')"
    )

    def matched(who):
        return [r["message_id"] for r in query_ex(statement, 10, scope=who)["rows"]]

    assert matched(OWNER) == [d1]
    assert matched(restricted("guest")) == [d1]
    # d2 derives from a hidden source, so the guest cannot see it, and it no
    # longer makes d1 match.
    owner(
        "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) VALUES ('message',%s,%s,1,'source')",
        (d2, source),
    )
    assert matched(OWNER) == [d1]
    assert matched(restricted("guest")) == []
