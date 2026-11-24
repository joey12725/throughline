"""Evidence class on every record and overlay scope plumbing
(INT-12). Synthetic data only."""

import warnings
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message
from psycopg.types.json import Jsonb

from throughline import passages, resultsets
from throughline.db import connect
from throughline.retrieval import RecallRequest, hydrate, recall, scope_sql
from throughline.retrieval import types as types_module
from throughline.retrieval.executor import RecallContext
from throughline.retrieval.legs import passages as passages_leg
from throughline.retrieval.legs import discover as discover_legs
from throughline.retrieval.types import (
    RECALL_KEYS,
    RECORD_CONTRACT,
    RECORD_KEYS,
    HardScope,
)
from throughline.scope import Scope

OWNER = Scope.local()
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAGE = "fake.page"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_") and any(
            part in name
            for part in ("EMBEDDING_", "RERANK", "DEFAULT_RANKER", "RECALL_", "SPAN")
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


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text


def mid(external_id, channel="synthetic.chat"):
    return owner(
        "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
        (channel, external_id),
    )[0]["id"]


def cite(derived_id, source_id, kind="message"):
    owner(
        "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT %s,%s,m.id,m.version,'source' FROM tl.messages m WHERE m.id=%s",
        (kind, derived_id, source_id),
    )


def link(from_id, to_kind, to_id, method="source", evidence=None):
    return owner(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence,method,job,job_version) VALUES ('about','message',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            from_id,
            to_kind,
            to_id,
            Jsonb(evidence or {}),
            method,
            None if method in ("source", "owner") else "synthetic-job",
            None if method in ("source", "owner") else "1",
        ),
    )[0]["id"]


def world(client):
    """One record of every kind the database has today, with its expected
    evidence class: {record_id: class}."""
    register(client, "fake.reference", is_reference=True)
    register(client, PAGE, is_reference=True, record_kind="page_revision")
    register(client, "fake.timeline", record_kind="derived")
    put(
        client,
        [
            message(0, content="orchard source words"),
            message(1, content="orchard second source"),
            message(2, channel="fake.reference", fields={}, content="orchard manual"),
            message(
                3,
                channel=PAGE,
                external_id="sourced",
                thread={"external_id": "sourced"},
                fields={},
                content="# Orchard\n\nSynthetic orchard page with sources.",
            ),
            message(
                4,
                channel=PAGE,
                external_id="bare",
                thread={"external_id": "bare"},
                fields={},
                content="# Bare\n\nSynthetic orchard page without sources.",
            ),
            message(5, channel="fake.timeline", fields={}, content="orchard visit"),
            message(6, channel="fake.timeline", fields={}, content="orchard guess"),
        ],
    )
    raw, second = mid("fake-0"), mid("fake-1")
    reference = mid("fake-2", "fake.reference")
    sourced, bare = mid("sourced", PAGE), mid("bare", PAGE)
    timeline, unsupported_timeline = (
        mid("fake-5", "fake.timeline"),
        mid("fake-6", "fake.timeline"),
    )
    cite(sourced, raw)
    cite(timeline, raw)
    with connect("app") as c:
        assert passages.build(c, 100) == 2
    passage = owner("SELECT id FROM tl.passages WHERE message_id=%s", (sourced,))[0][
        "id"
    ]
    bare_passage = owner("SELECT id FROM tl.passages WHERE message_id=%s", (bare,))[0][
        "id"
    ]
    farm = owner(
        "INSERT INTO tl.entities(kind,name) VALUES ('concept','Synthetic Farm') RETURNING id"
    )[0]["id"]
    source_link = link(raw, "message", second)
    entity_link = link(raw, "entity", farm)
    rule_link = link(raw, "entity", farm, "rule", {"message_ids": [second]})
    model_link = link(second, "entity", farm, "model", {"message_ids": [raw]})
    return {
        f"m:{raw}": "primary",
        f"m:{reference}": "primary",
        f"m:{sourced}": "normalized",
        f"m:{bare}": "inference",
        f"m:{timeline}": "normalized",
        f"m:{unsupported_timeline}": "inference",
        f"p:{passage}": "normalized",
        f"p:{bare_passage}": "inference",
        f"e:{farm}": "normalized",
        f"l:{source_link}": "normalized",
        f"l:{entity_link}": "normalized",
        f"l:{rule_link}": "derived",
        f"l:{model_link}": "inference",
    }


def test_p7_evidence_class_every_kind(client, channel):
    expected = world(client)
    records = hydrate.records_for_members(
        OWNER, [{"record_id": rid} for rid in expected]
    )
    assert {r["record_id"] for r in records} == set(expected)
    got = {r["record_id"]: r["evidence_class"] for r in records}
    assert got == expected
    rule = hydrate.evidence_class
    support = [dict(message_id=1, version=1, relation="supports")]
    # A generated link that exposes no method is 'derived' (documented).
    assert rule(dict(kind="link", generated=True, provenance={}, support=support)) == (
        "derived"
    )
    assert set(got.values()) <= set(types_module.EVIDENCE_CLASSES)
    # The generated flag is unchanged by the class.
    by_id = {r["record_id"]: r for r in records}
    assert by_id[next(k for k in expected if k.startswith("p:"))]["generated"] is True


def test_p7_evidence_class_identical_across_recall_and_result_sets(client, channel):
    expected = world(client)
    found = run(query="orchard", k=50, legs=["lexical", "passages"])
    assert {r["kind"] for r in found.records} >= {"life_event", "passage"}
    assert all(
        r["evidence_class"] == expected.get(r["record_id"], r["evidence_class"])
        for r in found.records
    )
    handle = resultsets.create(
        OWNER,
        [{"record_id": rid} for rid in expected],
        completeness="exhaustive",
    )
    stored = {
        r["record_id"]: r["evidence_class"]
        for r in resultsets.read(OWNER, handle)["records"]
    }
    assert stored == expected
    for record in found.records:
        assert (
            stored.get(record["record_id"], record["evidence_class"])
            == (record["evidence_class"])
        )
    assert all(list(r)[: len(RECORD_KEYS)] == list(RECORD_KEYS) for r in found.records)


def test_p7_record_contract_unchanged(client):
    assert "evidence_class" not in RECORD_CONTRACT
    # evidence_class was appended to the recall keys after the keys that
    # existed before it; later packages append after it (people:
    # attribution), never before.
    assert RECALL_KEYS[: RECALL_KEYS.index("evidence_class")] == (
        "span",
        "conversation",
        "score",
        "discovery",
        "role",
        "depends_on",
        "groups",
        "tokens",
    )
    assert RECORD_KEYS == RECORD_CONTRACT + RECALL_KEYS
    with connect() as c:
        sources = c.execute("SELECT prefix,relation FROM tl.record_sources").fetchall()
        assert {s["prefix"] for s in sources} >= {"m", "p", "l", "e"}
        for source in sources:
            cursor = c.execute(f"SELECT * FROM {source['relation']} LIMIT 0")
            assert tuple(col.name for col in cursor.description) == RECORD_CONTRACT


def test_unknown_kind_default_class_warns_once():
    hydrate._warned.discard("fake_future_kind")
    with pytest.warns(RuntimeWarning, match="fake_future_kind"):
        assert (
            hydrate.evidence_class(dict(kind="fake_future_kind", generated=False))
            == "primary"
        )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert (
            hydrate.evidence_class(
                dict(kind="fake_future_kind", generated=True, unsupported=False)
            )
            == "derived"
        )
    assert caught == []
    # A generated unsupported record of any kind is an inference.
    assert (
        hydrate.evidence_class(
            dict(kind="fake_future_kind", generated=True, unsupported=True)
        )
        == "inference"
    )
    assert set(hydrate.EVIDENCE_RULES) >= set(types_module.RECORD_KINDS)


# --- P4a overlay scope plumbing ----------------------------------------------------


def resolved(**scope):
    hard = HardScope(**scope)
    return scope_sql.Resolved(scope=hard, kinds=scope_sql.message_kinds(hard))


def test_include_ignored_renders_sql_kinds_without_rewriting_scope():
    plain = resolved()
    assert scope_sql.values(plain)["kinds"] is None
    flagged = resolved(include_ignored=True)
    assert flagged.scope.kinds is None and flagged.kinds is None
    assert scope_sql.values(flagged)["kinds"] == ["life_event", "derived", "ignored"]
    assert scope_sql.values(flagged, reduced=True)["kinds"] == [
        "life_event",
        "derived",
        "ignored",
    ]
    assert "ARRAY['life_event','derived','ignored']::text[]" in scope_sql.args(flagged)
    with_reference = resolved(include_ignored=True, include_reference=True)
    assert scope_sql.values(with_reference)["kinds"] == [
        "life_event",
        "derived",
        "reference",
        "ignored",
    ]
    explicit = resolved(include_ignored=True, kinds=["life_event"])
    assert scope_sql.values(explicit)["kinds"] == ["life_event", "ignored"]
    assert explicit.scope.kinds == ["life_event"]
    only_pages = resolved(include_ignored=True, kinds=["passage"])
    assert only_pages.kinds == []
    assert scope_sql.values(only_pages)["kinds"] == []
    assert scope_sql.values(only_pages, reduced=True)["kinds"] == []
    assert only_pages.scope.kinds == ["passage"]
    # Without the flag nothing changes.
    assert scope_sql.values(resolved(kinds=["life_event"]))["kinds"] == ["life_event"]


def test_include_ignored_keeps_passages_entity_and_link_legs_available(client):
    legs = discover_legs()
    for kinds in (None, ["passage", "entity", "link", "life_event"]):
        request = RecallRequest(
            query="orchard",
            scope=dict(include_ignored=True, kinds=kinds, message_ids=[1]),
        )
        ctx = RecallContext(OWNER, request)
        ctx.resolved = scope_sql.resolve(ctx, request.scope)
        for name in ("passages", "entity", "links", "lexical"):
            assert legs[name].available(ctx) == (True, None), (name, kinds)
        assert request.scope.kinds == kinds
    request = RecallRequest(
        query="orchard", scope=dict(include_ignored=True, kinds=["passage"])
    )
    ctx = RecallContext(OWNER, request)
    ctx.resolved = scope_sql.resolve(ctx, request.scope)
    assert passages_leg.LEG.available(ctx) == (True, None)
    assert legs["lexical"].available(ctx) == (False, "kinds_excluded")


def test_ignored_not_a_public_kind(client):
    with pytest.raises(ValueError):
        HardScope(kinds=["ignored"])
    with pytest.raises(ValueError):
        recall(OWNER, dict(query="orchard", scope=dict(kinds=["ignored"])))
    r = client.post(
        "/v1/recall", json=dict(query="orchard", scope=dict(kinds=["ignored"]))
    )
    assert r.status_code in (400, 422)
    assert "kinds must be among" in r.text
    ok = client.post(
        "/v1/recall", json=dict(query="orchard", scope=dict(include_ignored=True))
    )
    assert ok.status_code == 200, ok.text


def test_time_basis_effective_accepted_and_passages_use_record(client):
    scope = HardScope(time_basis="effective")
    assert scope_sql.values(resolved(time_basis="effective"))["time_basis"] == (
        "effective"
    )
    assert scope_sql.passage_time_basis(scope) == "record"
    assert scope_sql.passage_time_basis(HardScope(time_basis="subject")) == "subject"
    with pytest.raises(ValueError):
        HardScope(time_basis="bogus")
    register(client, PAGE, is_reference=True, record_kind="page_revision")
    put(
        client,
        [
            message(
                i,
                channel=PAGE,
                external_id=f"page-{i}",
                thread={"external_id": f"page-{i}"},
                fields={},
                content=f"# Page {i}\n\nSynthetic orchard page {i}.",
                happened_at=(AT + timedelta(days=10 * i)).isoformat(),
            )
            for i in range(3)
        ],
    )
    with connect("app") as c:
        assert passages.build(c, 100) == 3
    response = run(
        query="orchard",
        k=10,
        scope=dict(
            kinds=["passage"],
            time_basis="effective",
            after=(AT + timedelta(days=5)).isoformat(),
            before=(AT + timedelta(days=15)).isoformat(),
        ),
    )
    (passages_stage,) = [
        s for s in response.execution["stages"] if s["name"] == "passages"
    ]
    assert passages_stage["status"] == "ok"
    assert [r["external_id"] for r in response.records] == ["page-1"]


# --- optional-feature (migration 0042 overlay) -------------------------------------


def _overlay_present():
    return bool(
        owner("SELECT to_regclass('tl.annotations') IS NOT NULL AS ok")[0]["ok"]
    )


def test_p4_ignored_excluded_from_recall_by_default_and_included_with_flag(client):
    if not _overlay_present():
        pytest.skip("optional-feature: needs the overlay of migration 0042")
    register(
        client,
        "fake.wave",
        fields=[dict(name="ignore_reason", type="text", description="Synthetic")],
    )
    put(
        client,
        [
            message(0, channel="fake.wave", fields={}, content="orchard kept"),
            message(
                1,
                channel="fake.wave",
                fields={"ignore_reason": "noise"},
                content="orchard noise",
            ),
        ],
    )
    kept, noise = mid("fake-0", "fake.wave"), mid("fake-1", "fake.wave")
    default = run(query="orchard", legs=["lexical"], k=10)
    assert {r["message_id"] for r in default.records} == {kept}
    flagged = run(
        query="orchard", legs=["lexical"], k=10, scope=dict(include_ignored=True)
    )
    assert {r["message_id"] for r in flagged.records} == {kept, noise}


def test_p4_effective_time_basis_through_recall(client, channel):
    if not _overlay_present():
        pytest.skip("optional-feature: needs the overlay of migration 0042")
    put(
        client,
        [message(0, content="orchard dated"), message(1, content="orchard other")],
    )
    dated = mid("fake-0")
    channel_id = owner("SELECT id FROM tl.channels WHERE name='synthetic.chat'")[0][
        "id"
    ]
    owner(
        "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) VALUES (%s,'fake-0','effective_time',%s,'owner','owner')",
        (channel_id, Jsonb({"at": "2019-06-01T00:00:00Z", "precision": "day"})),
    )
    window = dict(after="2019-05-01T00:00:00Z", before="2019-07-01T00:00:00Z")
    effective = run(
        query="orchard",
        legs=["lexical"],
        k=10,
        scope=dict(window, time_basis="effective"),
    )
    assert {r["message_id"] for r in effective.records} == {dated}
    by_record = run(query="orchard", legs=["lexical"], k=10, scope=window)
    assert by_record.records == []


def test_p7_evidence_rules_from_contract_fields_only():
    """The rule table on synthetic contract rows (checker addition)."""
    ec = hydrate.evidence_class
    assert ec(dict(kind="life_event", generated=False)) == "primary"
    assert ec(dict(kind="reference", generated=False)) == "primary"
    assert ec(dict(kind="life_event", generated=True, unsupported=False)) == "derived"
    assert ec(dict(kind="entity", generated=False)) == "normalized"
    assert ec(dict(kind="entity", generated=True, unsupported=False)) == "derived"
    assert ec(dict(kind="link", generated=False, provenance={"method": "source"})) == (
        "normalized"
    )
    assert ec(dict(kind="link", generated=True, provenance={"method": "rule"})) == (
        "derived"
    )
    assert ec(dict(kind="link", generated=True, provenance={})) == "derived"
    assert ec(dict(kind="link", generated=True, provenance={"method": "model"})) == (
        "inference"
    )
    assert ec(dict(kind="passage", generated=True, unsupported=True)) == "inference"
