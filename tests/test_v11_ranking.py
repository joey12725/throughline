"""Pluggable ranking, cached scores, message spans and page passages inside
recall (QRY-17, QRY-20, QRY-22, QRY-23, QRY-25, CST-5, BRN-5, BRN-6).

Synthetic data only. Every provider is the deterministic local one or a fake
defined here; nothing leaves the host.
"""

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message

from throughline import access, embeddings, operators, passages, providers
from throughline.db import connect
from throughline.derived import materialize
from throughline.providers.base import Reranker
from throughline.providers.local import LocalLexicalReranker
from throughline.retrieval import RecallRequest, default_planner, ranking, recall
from throughline.retrieval import legs as legs_module
from throughline.retrieval.executor import RecallContext
from throughline.retrieval.legs import Leg
from throughline.retrieval.legs import passages as passages_leg
from throughline.retrieval.stages import spans as spans_stage
from throughline.retrieval.types import Candidate
from throughline.scope import Scope

recall_module = importlib.import_module("throughline.retrieval.recall")

OWNER = Scope.local()
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAGE = "fake.page"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    """No provider configuration leaks in from the machine running the tests."""
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_") and any(
            part in name
            for part in (
                "EMBEDDING_",
                "RERANK",
                "SEMANTIC_",
                "CONTROLLER_",
                "MODEL_",
                "EGRESS_",
                "RRF_K",
                "RECALL_",
                "RETRIEVAL_",
                "LEG_TIMEOUT",
                "DEFAULT_RANKER",
                "SPAN",
                "SCOPE_SOURCE_CAP",
            )
        ):
            monkeypatch.delenv(name)


# --- helpers ---------------------------------------------------------------------


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


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return r.json()


def ids(channel="synthetic.chat"):
    rows = owner(
        "SELECT m.id, m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id",
        (channel,),
    )
    return {r["external_id"]: r["id"] for r in rows}


def stage(response, name):
    return [s for s in response.execution["stages"] if s["name"] == name]


def record_ids(response):
    return [r["record_id"] for r in response.records]


def embed_all(monkeypatch, dimensions="64"):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", dimensions)
    materialize(wait=True)
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
    try:
        access.register_asker(asker)
    except Exception:
        pass
    access.grant(asker, channel=channel)


def page_channel(client, name=PAGE):
    return register(client, name, is_reference=True, record_kind="page_revision")


def page(i, external_id, content, at=None, channel=PAGE, thread=None):
    return message(
        i,
        channel=channel,
        external_id=external_id,
        thread={"external_id": thread or external_id},
        content=content,
        happened_at=(at or AT + timedelta(seconds=i)).isoformat(),
        fields={},
    )


def build():
    with connect("app") as c:
        return passages.build(c, 1000)


def message_id(external_id, version=None, channel=PAGE):
    with connect() as c:
        row = c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND (%s::int IS NULL OR m.version=%s::int) ORDER BY m.version DESC LIMIT 1",
            (channel, external_id, version, version),
        ).fetchone()
    return row["id"]


def cite(derived_id, source_id, kind="message", origin="source"):
    owner(
        "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT %s,%s,m.id,m.version,%s FROM tl.messages m WHERE m.id=%s",
        (kind, derived_id, origin, source_id),
    )


def passage_ids(message_id_):
    return [
        r["id"]
        for r in owner(
            "SELECT id FROM tl.passages WHERE message_id=%s ORDER BY ordinal",
            (message_id_,),
        )
    ]


class CountingReranker(LocalLexicalReranker):
    """The local lexical reranker, counting calls and the texts it scored."""

    def __init__(self):
        self.calls = 0
        self.texts = []

    def score(self, query, passages_):
        self.calls += 1
        self.texts.extend(passages_)
        return super().score(query, passages_)


class FailingReranker(LocalLexicalReranker):
    def score(self, query, passages_):
        raise providers.ProviderUnavailable("synthetic outage")


class RemoteReranker(Reranker):
    """A fake non-local reranker: priced, never contacted over a network."""

    name = "fake-remote"
    version = "fake-remote-v1"
    model = "fake-remote-model"
    local = False

    def __init__(self, price_per_million):
        self.price_per_million = price_per_million
        self.calls = 0

    def score(self, query, passages_):
        self.calls += 1
        scores, _ = LocalLexicalReranker().score(query, passages_)
        return scores, self._usage(sum(len(p) for p in passages_) // 4)


def ranked_messages(client, texts, thread_per_message=True):
    put(
        client,
        [
            message(
                i * 4000,
                content=text,
                thread={"external_id": f"fake-t{i}"}
                if thread_per_message
                else {"external_id": "fake-thread"},
            )
            for i, text in enumerate(texts)
        ],
    )
    return ids()


TEXTS = [
    "orchard orchard orchard harvest",
    "ladder by the orchard and the apples",
    "orchard apples ladder morning",
    "an orchard note",
    "apples ladder orchard row",
]
QUERY = "orchard or apples or ladder"


# --- QRY-23 rankers ------------------------------------------------------------


def test_qry_23_rankers_selectable_and_reported(client, channel, monkeypatch):
    ranked_messages(client, TEXTS)
    for name in ("fused", "lexical", "vector", "cross_encoder"):
        response = run(query=QUERY, legs=["lexical"], k=10, ranker=name)
        reported = response.execution["ranker"]
        assert reported["name"] == name
        assert reported["exact"] is (name != "cross_encoder")
        reranked = stage(response, "rerank")
        if name == "fused":
            assert reranked == []
        else:
            assert reranked[0]["status"] == "ok"
            assert reranked[0]["exact"] is (name != "cross_encoder")
            for record in response.records:
                entry = [d for d in record["discovery"] if d["leg"] == "rerank"]
                assert entry and entry[0]["detail"]["ranker"] == name
                assert entry[0]["detail"]["version"] == reported["version"]
    cross = run(query=QUERY, legs=["lexical"], k=10, ranker="cross_encoder")
    reported = cross.execution["ranker"]
    local = providers.get("rerank")
    assert reported["provider"] == local.name
    assert reported["provider_version"] == local.version
    assert local.version in reported["version"]
    assert cross.execution["exactness"] == "mixed"
    # Default from the environment; the request wins.
    monkeypatch.setenv("THROUGHLINE_DEFAULT_RANKER", "lexical")
    assert run(query=QUERY, legs=["lexical"]).execution["ranker"]["name"] == "lexical"
    assert (
        run(query=QUERY, legs=["lexical"], ranker="fused").execution["ranker"]["name"]
        == "fused"
    )
    monkeypatch.setenv("THROUGHLINE_DEFAULT_RANKER", "no-such-ranker")
    fallback = run(query=QUERY, legs=["lexical"])
    assert fallback.execution["ranker"]["name"] == "fused"
    assert any("DEFAULT_RANKER" in w for w in fallback.execution["warnings"])
    monkeypatch.delenv("THROUGHLINE_DEFAULT_RANKER")
    # An unknown ranker is the caller's error.
    with pytest.raises(ValueError):
        run(query=QUERY, ranker="no-such-ranker")
    refused = client.post("/v1/recall", json=dict(query=QUERY, ranker="no-such"))
    assert refused.status_code == 400
    assert "unknown ranker" in refused.json()["detail"]
    ok = client.post(
        "/v1/recall", json=dict(query=QUERY, legs=["lexical"], ranker="lexical")
    )
    assert ok.status_code == 200 and ok.json()["execution"]["ranker"]["name"] == (
        "lexical"
    )
    # Every ranker is an operator implementing 'rank'.
    specs = {s.name: s for s in operators.equivalents("rank", exact_only=False)}
    assert {
        "ranker.fused",
        "ranker.lexical",
        "ranker.vector",
        "ranker.cross_encoder",
        "ranker.learned",
    } <= set(specs)
    assert specs["ranker.cross_encoder"].exact is False
    assert specs["ranker.fused"].exact is True


def test_qry_23_same_candidates_different_rankers(client, channel):
    ranked_messages(client, TEXTS)
    fused = run(query=QUERY, legs=["lexical"], k=10)
    lexical = run(query=QUERY, legs=["lexical"], k=10, ranker="lexical")
    cross = run(query=QUERY, legs=["lexical"], k=10, ranker="cross_encoder")
    assert len(fused.records) == len(TEXTS)
    assert set(record_ids(fused)) == set(record_ids(lexical)) == set(record_ids(cross))
    # lexical: by the keyword leg's own score.
    keyword = [
        next(d["score"] for d in r["discovery"] if d["leg"] == "lexical")
        for r in lexical.records
    ]
    assert keyword == sorted(keyword, reverse=True)
    # cross_encoder: by the rerank provider's score, stable on the fused order.
    texts = {r["record_id"]: r["content"] for r in fused.records}
    scores, _ = LocalLexicalReranker().score(
        QUERY, [texts[i] for i in record_ids(fused)]
    )
    expected = [
        rid
        for _, _, rid in sorted(
            (-s, n, rid) for n, (s, rid) in enumerate(zip(scores, record_ids(fused)))
        )
    ]
    assert record_ids(cross) == expected
    ranks = [
        next(d["rank"] for d in r["discovery"] if d["leg"] == "rerank")
        for r in cross.records
    ]
    assert ranks == list(range(1, len(TEXTS) + 1))
    assert record_ids(cross) != record_ids(fused)


def test_qry_23_learned_placeholder_degrades(client, channel):
    ranked_messages(client, TEXTS)
    fused = run(query=QUERY, legs=["lexical"], k=10)
    learned = run(query=QUERY, legs=["lexical"], k=10, ranker="learned")
    assert record_ids(learned) == record_ids(fused)
    (failed,) = stage(learned, "rerank")
    assert failed["status"] == "failed"
    assert failed["reason"] == "provider_unavailable"
    assert learned.execution["status"] == "degraded"
    assert {"stage": "rerank", "reason": "provider_unavailable"} in learned.execution[
        "incomplete"
    ]
    assert all(d["leg"] != "rerank" for r in learned.records for d in r["discovery"])


def test_qry_22_reranker_failure_keeps_order_and_discloses(client, channel):
    ranked_messages(client, TEXTS)
    fused = run(query=QUERY, legs=["lexical"], k=10)
    for provider in (None, FailingReranker()):
        with providers.override("rerank", provider):
            response = run(query=QUERY, legs=["lexical"], k=10, ranker="cross_encoder")
        assert record_ids(response) == record_ids(fused)
        (failed,) = stage(response, "rerank")
        assert failed["status"] == "failed"
        assert failed["reason"] == "provider_unavailable"
        assert response.execution["status"] == "degraded"
        assert len(response.records) == len(TEXTS)


def test_qry_23_non_local_reranker_needs_budget(client, channel):
    ranked_messages(client, TEXTS)
    fused = run(query=QUERY, legs=["lexical"], k=10)
    expensive = RemoteReranker(price_per_million=1e7)
    with providers.override("rerank", expensive):
        response = run(query=QUERY, legs=["lexical"], k=10, ranker="cross_encoder")
    assert expensive.calls == 0  # nothing was sent
    (failed,) = stage(response, "rerank")
    assert failed["reason"] == "approval_required"
    assert failed["approval_id"]
    assert record_ids(response) == record_ids(fused)
    pending = owner(
        "SELECT status, cost FROM tl.approvals WHERE id=%s", (failed["approval_id"],)
    )
    assert pending[0]["status"] == "pending" and float(pending[0]["cost"]) > 0.10
    # A caller budget of zero refuses even a cheap remote call.
    cheap = RemoteReranker(price_per_million=1.0)
    with providers.override("rerank", cheap):
        refused = run(
            query=QUERY,
            legs=["lexical"],
            k=10,
            ranker="cross_encoder",
            budgets=dict(model_cost=0),
        )
        assert cheap.calls == 0
        assert stage(refused, "rerank")[0]["reason"] == "approval_required"
        allowed = run(query=QUERY, legs=["lexical"], k=10, ranker="cross_encoder")
    assert cheap.calls == 1
    assert stage(allowed, "rerank")[0]["status"] == "ok"
    assert allowed.execution["budget"]["spent"]["model_cost"] > 0
    assert allowed.execution["ranker"]["local"] is False
    rows = owner(
        "SELECT provider, purpose FROM tl.model_ledger WHERE provider='fake-remote'"
    )
    assert rows and {r["purpose"] for r in rows} == {"rerank"}


# --- CST-5 cached scores --------------------------------------------------------


def conversation(client, contents, start=0, thread="fake-thread", **kwargs):
    put(
        client,
        [
            message(start + i, content=text, thread={"external_id": thread}, **kwargs)
            for i, text in enumerate(contents)
        ],
    )


CONVERSATION = [
    "the orchard ladder is out",
    "quiet morning walk",
    "nothing much today",
    "the orchard is quiet",
    "a ladder stands there",
    "weather report",
    "more chatter",
    "orchard ladder again",
]


def _ctx(scope, query):
    request = RecallRequest(query=query)
    return RecallContext(scope, request)


def test_cst_5_rerank_and_span_scores_cached(client, channel, monkeypatch):
    conversation(client, CONVERSATION)
    embed_all(monkeypatch)
    counter = CountingReranker()
    with providers.override("rerank", counter):
        first = run(
            query="orchard ladder",
            legs=["vector", "lexical"],
            k=20,
            ranker="cross_encoder",
        )
        assert stage(first, "spans")[0]["status"] == "ok"
        assert stage(first, "rerank")[0]["status"] == "ok"
        assert counter.calls == 2  # spans and rerank, one batch each
        scored = len(counter.texts)
        second = run(
            query="orchard ladder",
            legs=["vector", "lexical"],
            k=20,
            ranker="cross_encoder",
        )
        assert counter.calls == 2 and len(counter.texts) == scored
        assert record_ids(second) == record_ids(first)
        # Editing one message: only texts containing it are scored again.
        edited = ingest(client, [message(4, content="a ladder stands by the gate")])
        assert edited.json()["new_versions"] == 1
        embed_all(monkeypatch)
        counter.texts.clear()
        run(
            query="orchard ladder",
            legs=["vector", "lexical"],
            k=20,
            ranker="cross_encoder",
        )
        assert counter.texts
        assert all("by the gate" in text for text in counter.texts)
    cached = owner("SELECT operator, value FROM tl.semantic_cache")
    assert {r["operator"] for r in cached} >= {"spans", "cross_encoder"}

    # A cached score is never served for text with a hidden source message.
    register(client, "fake.hidden")
    put(client, [message(100, channel="fake.hidden", fields={}, content="secret")])
    hidden = ids("fake.hidden")["fake-100"]
    shown = ids()["fake-0"]
    grant("guest", "synthetic.chat")
    counter = CountingReranker()
    items = [
        dict(text="orchard text one", message_ids=[hidden]),
        dict(text="orchard text two", message_ids=[shown]),
    ]
    with providers.override("rerank", counter):
        scores, stats = ranking.score_cached(_ctx(OWNER, "orchard"), "orchard", items)
        assert counter.calls == 1 and stats["misses"] == 2
        again, stats = ranking.score_cached(_ctx(OWNER, "orchard"), "orchard", items)
        assert again == scores and counter.calls == 1 and stats["hits"] == 2
        guest, stats = ranking.score_cached(
            _ctx(restricted("guest"), "orchard"), "orchard", items
        )
        assert guest == scores
        assert counter.calls == 2 and stats["hits"] == 1 and stats["misses"] == 1
        assert counter.texts[-1:] == ["orchard text one"]


# --- QRY-25 spans ----------------------------------------------------------------


def _conversation_records(response, conversation_id):
    return [
        r
        for r in response.records
        if r["record_id"].startswith("m:")
        and (r["conversation"] or {}).get("id") == conversation_id
    ]


def test_qry_25_multiple_minimal_spans_per_conversation(client, channel, monkeypatch):
    conversation(client, CONVERSATION)
    embed_all(monkeypatch)
    m = ids()
    response = run(query="orchard ladder", legs=["vector"], k=20)
    assert stage(response, "spans")[0]["status"] == "ok"
    (conv,) = {r["conversation"]["id"] for r in response.records}
    found = _conversation_records(response, conv)
    by_id = {r["message_id"]: r for r in found}
    numbers = {
        r["id"]: r["thread_no"] for r in owner("SELECT id, thread_no FROM tl.messages")
    }
    # Two single-message spans with both terms and one two-message span.
    assert len(found) == 3
    assert set(by_id) == {m["fake-0"], m["fake-7"], m["fake-3"]} or set(by_id) == {
        m["fake-0"],
        m["fake-7"],
        m["fake-4"],
    }
    for single in (m["fake-0"], m["fake-7"]):
        span = by_id[single]["span"]
        assert span["first_no"] == span["last_no"] == numbers[single]
    pair = next(r for r in found if r["message_id"] not in (m["fake-0"], m["fake-7"]))
    assert pair["span"] == dict(
        first_no=numbers[m["fake-3"]], last_no=numbers[m["fake-4"]]
    )
    # Spans never overlap, and every one keeps the conversation hit's discovery.
    ranges = sorted((r["span"]["first_no"], r["span"]["last_no"]) for r in found)
    assert all(a[1] < b[0] for a, b in zip(ranges, ranges[1:]))
    assert all([d["leg"] for d in r["discovery"]] == ["vector"] for r in found)
    # The limit per conversation applies.
    monkeypatch.setenv("THROUGHLINE_SPANS_PER_CONVERSATION", "1")
    one = run(query="orchard ladder", legs=["vector"], k=20)
    assert len(_conversation_records(one, conv)) == 1
    # No query term in the conversation: the single representative stays.
    plain = run(query="zebra", legs=["vector"], k=20)
    assert len(plain.records) == 1
    assert plain.records[0]["span"]["first_no"] == plain.records[0]["span"]["last_no"]


def test_qry_25_single_span_opt_out_matches_legacy(client, channel, monkeypatch):
    conversation(client, CONVERSATION)
    conversation(client, ["orchard ladder elsewhere", "ladder"], 100, "fake-other")
    embed_all(monkeypatch)
    multi = run(query="orchard ladder", legs=["vector"], k=20)
    single = run(
        query="orchard ladder", legs=["vector"], k=20, hints={"single_span": True}
    )
    unexpanded = run(query="orchard ladder", legs=["vector"], k=20, expand=False)
    monkeypatch.setattr(spans_stage.STAGE, "enabled", lambda ctx: False)
    legacy = run(query="orchard ladder", legs=["vector"], k=20)
    monkeypatch.undo()
    assert stage(single, "spans") == [] and stage(unexpanded, "spans") == []
    assert record_ids(single) == record_ids(unexpanded) == record_ids(legacy)
    assert [r["score"] for r in single.records] == [r["score"] for r in legacy.records]
    # Legacy: one representative per conversation.
    conversations = [r["conversation"]["id"] for r in legacy.records]
    assert len(conversations) == len(set(conversations)) == 2
    assert len(multi.records) > len(single.records)


def test_qry_25_no_stored_message_vectors(client, channel, monkeypatch):
    conversation(client, CONVERSATION)
    embed_all(monkeypatch)
    before = owner("SELECT count(*) n FROM tl.embeddings")[0]["n"]
    conversations = owner("SELECT count(*) n FROM tl.conversations")[0]["n"]
    embedded = []
    real = embeddings.embed

    def counting(texts, model=None, dimensions=None):
        embedded.append(list(texts))
        return real(texts, model=model, dimensions=dimensions)

    monkeypatch.setattr(embeddings, "embed", counting)
    response = run(query="orchard ladder", legs=["vector"], k=20)
    assert stage(response, "spans")[0]["status"] == "ok"
    assert len(response.records) > 1
    # One vector per conversation, before and after; at most the query is
    # embedded, never a message or a window.
    assert owner("SELECT count(*) n FROM tl.embeddings")[0]["n"] == before
    assert before == conversations
    assert all(batch == ["orchard ladder"] for batch in embedded)
    spans_cached = owner("SELECT value FROM tl.semantic_cache WHERE operator='spans'")
    assert spans_cached and all(set(r["value"]) == {"score"} for r in spans_cached)


def test_qry_20_span_anchor_respects_person_and_time_scope(
    client, channel, monkeypatch
):
    p, q = person("synthetic-p"), person("synthetic-q")
    conversation(client, [f"orchard ladder turn {i}" for i in range(10)])
    m = ids()
    for i in range(10):
        owner(
            "UPDATE tl.messages SET person_id=%s WHERE id=%s",
            (p if i % 2 == 0 else q, m[f"fake-{i}"]),
        )
    embed_all(monkeypatch)
    after, before = AT + timedelta(seconds=2), AT + timedelta(seconds=7)
    scope = dict(persons=[p], after=after.isoformat(), before=before.isoformat())
    response = run(query="orchard ladder", legs=["vector"], k=20, scope=scope)
    assert stage(response, "spans")[0]["status"] == "ok"
    hits = [r for r in response.records if r["role"] == "hit"]
    assert len(hits) >= 2
    for record in hits:
        assert record["person_id"] == p
        assert after <= record["record_time"] <= before
    allowed = {m[f"fake-{i}"] for i in (2, 4, 6)}
    assert {r["message_id"] for r in hits} <= allowed


# --- BRN-5 / BRN-6 passages ---------------------------------------------------------


def test_brn_6_recall_returns_passage_with_revision_offsets_and_sources(client):
    page_channel(client)
    register(client, "fake.life")
    text = "# Orchard\n\nIntro line.\n\n## Ladder\n\nThe synthetic quokkaladder note."
    put(
        client,
        [
            message(50, channel="fake.life", external_id="src", fields={}),
            page(0, "wiki", text),
        ],
    )
    revision, source = message_id("wiki"), message_id("src", channel="fake.life")
    cite(revision, source)
    assert build() == 1
    response = run(query="quokkaladder", legs=["passages"], k=10)
    (record,) = response.records
    assert record["record_id"].startswith("p:") and record["kind"] == "passage"
    assert record["message_id"] == revision and record["version"] == 1
    assert record["channel"] == PAGE and record["external_id"] == "wiki"
    assert record["heading_path"][0] == "Orchard"
    assert text[record["char_start"] : record["char_end"]] == record["content"]
    assert "quokkaladder" in record["content"]
    assert record["support"] == [
        dict(message_id=source, version=1, relation="derived_from")
    ]
    assert record["generated"] is True and record["unsupported"] is False
    assert record["expansion"]["revision"] == revision
    assert record["evidence_class"] == "normalized"
    (entry,) = record["discovery"]
    assert entry["leg"] == "passages" and entry["rank"] == 1
    assert entry["detail"]["keyword_rank"] == 1
    assert stage(response, "passages")[0]["exact"] is True


def _wiki_revisions(client):
    page_channel(client)
    for day, word in ((1, "alpha"), (10, "beta"), (20, "gamma")):
        put(
            client,
            [
                page(
                    0,
                    "wiki",
                    f"# Wiki\n\nSynthetic orchard {word} revision.",
                    AT + timedelta(days=day),
                )
            ],
        )
    assert build() == 3
    return {
        r["version"]: r["id"]
        for r in owner(
            "SELECT m.id, m.version FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s",
            (PAGE,),
        )
    }


def _revisions(response):
    return {r["message_id"] for r in response.records if r["kind"] == "passage"}


def test_brn_5_default_current_and_as_of_revision(client):
    revisions = _wiki_revisions(client)
    current = run(query="orchard", legs=["passages"], k=10)
    assert _revisions(current) == {revisions[3]}
    old = run(
        query="orchard",
        legs=["passages"],
        k=10,
        scope=dict(as_of=(AT + timedelta(days=12)).isoformat()),
    )
    assert _revisions(old) == {revisions[2]}
    assert old.execution["as_of_honoured"] == {"passages": True}
    assert all(r["is_current"] is False for r in old.records)
    # Text of a later revision is not matched under as_of.
    assert (
        run(
            query="gamma",
            legs=["passages"],
            k=10,
            scope=dict(as_of=(AT + timedelta(days=12)).isoformat()),
        ).records
        == []
    )
    assert run(query="alpha", legs=["passages"], k=10).records == []


def test_brn_5_authorized_revision_through_recall(client):
    page_channel(client)
    register(client, "fake.public")
    register(client, "fake.secret")
    put(
        client,
        [
            message(50, channel="fake.public", external_id="pub", fields={}),
            message(51, channel="fake.secret", external_id="sec", fields={}),
        ],
    )
    put(client, [page(0, "wiki", "Synthetic orchard alpha revision.", AT)])
    put(
        client,
        [page(0, "wiki", "Synthetic orchard beta revision.", AT + timedelta(days=1))],
    )
    first, latest = message_id("wiki", 1), message_id("wiki", 2)
    public = message_id("pub", channel="fake.public")
    secret = message_id("sec", channel="fake.secret")
    cite(first, public)
    cite(latest, public)
    cite(latest, secret)
    assert build() == 2
    grant("guest", PAGE)
    grant("guest", "fake.public")
    assert _revisions(run(query="orchard", legs=["passages"])) == {latest}
    guest = run(restricted("guest"), query="orchard", legs=["passages"], k=10)
    assert _revisions(guest) == {first}
    (record,) = guest.records
    assert "alpha" in record["content"]
    assert run(restricted("guest"), query="beta", legs=["passages"]).records == []


def test_qry_20_passages_honour_time_thread_terms_and_person_scope_via_sources(
    client,
):
    page_channel(client)
    register(client, "fake.life")
    p, q = person("synthetic-p"), person("synthetic-q")
    put(
        client,
        [
            message(50, channel="fake.life", external_id="by-p", fields={}),
            message(51, channel="fake.life", external_id="by-q", fields={}),
            page(0, "early", "Synthetic orchard early page with ladder.", AT),
            page(1, "late", "Synthetic orchard late page.", AT + timedelta(days=30)),
            page(
                2,
                "other",
                "Synthetic orchard other page with ladder.",
                AT + timedelta(days=1),
                thread="fake-other-thread",
            ),
        ],
    )
    by_p = message_id("by-p", channel="fake.life")
    by_q = message_id("by-q", channel="fake.life")
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (p, by_p))
    owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (q, by_q))
    early, late, other = message_id("early"), message_id("late"), message_id("other")
    cite(early, by_p)
    cite(late, by_q)
    cite(other, by_q)
    assert build() == 3

    def found(**scope):
        return _revisions(run(query="orchard", legs=["passages"], k=10, scope=scope))

    assert found() == {early, late, other}
    assert found(before=(AT + timedelta(days=5)).isoformat()) == {early, other}
    assert found(after=(AT + timedelta(days=5)).isoformat()) == {late}
    thread = owner("SELECT thread_id FROM tl.messages WHERE id=%s", (other,))[0][
        "thread_id"
    ]
    assert found(thread_ids=[thread]) == {other}
    assert found(must_terms=["ladder"]) == {early, other}
    assert found(channels=["fake.life"]) == set()
    # Persons reach passages through their sources.
    assert found(persons=[p]) == {early}
    assert found(persons=[q]) == {late, other}
    assert found(persons=[q], must_terms=["ladder"]) == {other}
    # A person with no in-scope source finds no passage (never an unscoped one).
    nobody = person("synthetic-nobody")
    assert found(persons=[nobody]) == set()
    excluded = run(
        query="orchard",
        legs=["passages"],
        k=10,
        exclude_seen=[early],
    )
    assert early not in _revisions(excluded)


def test_passages_leg_skipped_with_reason_when_scope_too_broad(client, monkeypatch):
    page_channel(client)
    register(client, "fake.life")
    p = person("synthetic-p")
    put(
        client,
        [
            message(50 + i, channel="fake.life", external_id=f"s{i}", fields={})
            for i in range(3)
        ]
        + [page(0, "wiki", "Synthetic orchard page.")],
    )
    for i in range(3):
        source = message_id(f"s{i}", channel="fake.life")
        owner("UPDATE tl.messages SET person_id=%s WHERE id=%s", (p, source))
        cite(message_id("wiki"), source)
    assert build() == 1
    assert run(query="orchard", legs=["passages"], scope=dict(persons=[p])).records
    monkeypatch.setenv("THROUGHLINE_SCOPE_SOURCE_CAP", "2")
    response = run(query="orchard", legs=["passages"], scope=dict(persons=[p]))
    assert response.records == []
    (skipped,) = stage(response, "passages")
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "scope_too_broad_for_passages"
    assert response.execution["status"] == "degraded"


class AllLeg(Leg):
    """Test leg naming fixed message records, visible or not."""

    name = "fake_all"
    exact = True

    def __init__(self, ids_):
        self.ids = ids_

    def run(self, ctx):
        return [
            Candidate(record_id=f"m:{i}", message_id=i, score=1.0 / (n + 1))
            for n, i in enumerate(self.ids)
        ]


def test_acl_8_only_visible_text_reaches_reranker(client, channel):
    register(client, "fake.hidden")
    put(
        client,
        [message(0, content="orchard visible words")]
        + [
            message(
                10 + i,
                channel="fake.hidden",
                fields={},
                content=f"orchard zebrasecret {i}",
            )
            for i in range(2)
        ],
    )
    shown, hidden = ids(), ids("fake.hidden")
    grant("guest", "synthetic.chat")
    leg = AllLeg(list(hidden.values()) + list(shown.values()))
    legs_module.register(leg)
    counter = CountingReranker()
    try:
        with providers.override("rerank", counter):
            response = run(
                restricted("guest"),
                query="orchard",
                legs=["fake_all"],
                k=10,
                ranker="cross_encoder",
            )
            assert stage(response, "rerank")[0]["status"] == "ok"
            assert len(counter.texts) == 1
            assert "orchard visible words" in counter.texts[0]
            assert "zebrasecret" not in counter.texts[0]
            assert record_ids(response) == [f"m:{shown['fake-0']}"]
            # The owner's text does reach it.
            run(query="orchard", legs=["fake_all"], k=10, ranker="cross_encoder")
            assert any("zebrasecret" in t for t in counter.texts)
    finally:
        legs_module.unregister("fake_all")


def test_qry_13_passages_and_raw_fused(client, channel, monkeypatch):
    page_channel(client)
    put(
        client,
        [
            message(0, content="orchard harvest notes"),
            message(1, content="orchard ladder story"),
            page(2, "wiki", "# Wiki\n\nSynthetic orchard page text."),
        ],
    )
    assert build() == 1
    response = run(query="orchard", legs=["lexical", "passages"], k=10)
    kinds = [r["kind"] for r in response.records]
    assert set(kinds) == {"life_event", "passage"}
    assert kinds.count("passage") == 1
    # Fusion keeps every leg's rank; scores are reciprocal ranks.
    passage = next(r for r in response.records if r["kind"] == "passage")
    assert passage["discovery"][0]["leg"] == "passages"
    assert passage["score"] == pytest.approx(1 / 61)
    first = response.records[0]
    assert first["score"] == pytest.approx(1 / 61)
    # strategy auto includes the passages leg (the default planner).
    monkeypatch.setattr(recall_module, "planner", lambda: default_planner.PLANNER)
    auto = run(query="orchard", k=10)
    assert stage(auto, "passages")[0]["status"] == "ok"
    assert "passage" in {r["kind"] for r in auto.records}
    # kinds that exclude pages switch the leg off.
    narrow = run(query="orchard", legs=["passages"], scope=dict(kinds=["life_event"]))
    assert stage(narrow, "passages")[0]["reason"] == "kinds_excluded"
    listed = run(
        query="orchard", legs=["passages"], scope=dict(kinds=["page_revision"])
    )
    assert {r["kind"] for r in listed.records} == {"passage"}


def test_raw_only_excludes_passages(client, channel, monkeypatch):
    page_channel(client)
    put(
        client,
        [
            message(0, content="orchard notes"),
            page(1, "wiki", "Synthetic orchard page."),
        ],
    )
    assert build() == 1
    monkeypatch.setattr(recall_module, "planner", lambda: default_planner.PLANNER)
    for legs in (None, ["lexical", "passages"]):
        response = run(query="orchard", legs=legs, k=10, raw_only=True)
        assert {r["kind"] for r in response.records} == {"life_event"}
        assert all(s["status"] != "ok" for s in stage(response, "passages"))
    ctx = _ctx(OWNER, "orchard")
    ctx.request = RecallRequest(query="orchard", raw_only=True)
    from throughline.retrieval import scope_sql

    ctx.resolved = scope_sql.resolve(ctx, ctx.request.scope)
    assert passages_leg.LEG.available(ctx) == (False, "raw_only")


# --- degraded scoring paths (checker additions) --------------------------------------


def test_qry_22_spans_failure_keeps_representative_and_discloses(
    client, channel, monkeypatch
):
    conversation(client, CONVERSATION)
    embed_all(monkeypatch)
    legacy = run(
        query="orchard ladder", legs=["vector"], k=20, hints={"single_span": True}
    )
    with providers.override("rerank", FailingReranker()):
        failed = run(query="orchard ladder", legs=["vector"], k=20)
    (entry,) = stage(failed, "spans")
    assert entry["status"] == "failed" and entry["reason"] == "provider_unavailable"
    assert record_ids(failed) == record_ids(legacy)
    assert failed.execution["status"] == "degraded"
    # A non-local provider over budget: nothing is sent, the approval is named.
    expensive = RemoteReranker(price_per_million=1e7)
    with providers.override("rerank", expensive):
        pending = run(query="orchard ladder", legs=["vector"], k=20)
    assert expensive.calls == 0
    (entry,) = stage(pending, "spans")
    assert entry["reason"] == "approval_required" and entry["approval_id"]
    assert record_ids(pending) == record_ids(legacy)


class _Expired:
    """A tracker whose deadline passes after hydration, before scoring."""

    def remaining_ms(self):
        return 10_000.0

    def expired(self):
        return True


def test_qry_22_rerank_deadline_keeps_order_with_reason_timeout(client, channel):
    from throughline.retrieval import scope_sql
    from throughline.retrieval.executor import StageFailed
    from throughline.retrieval.stages import rerank as rerank_stage

    ranked_messages(client, TEXTS)
    ctx = _ctx(OWNER, QUERY)
    ctx.request = RecallRequest(query=QUERY, ranker="cross_encoder")
    ctx.resolved = scope_sql.resolve(ctx, ctx.request.scope)
    ctx.ranker = ranking.get("cross_encoder")
    candidates = [
        Candidate(record_id=f"m:{i}", message_id=i, score=1.0) for i in ids().values()
    ]
    counter = CountingReranker()
    ctx.tracker = _Expired()
    with providers.override("rerank", counter):
        with pytest.raises(StageFailed) as raised:
            rerank_stage.STAGE.run(ctx, candidates)
    assert raised.value.reason == "timeout"
    assert counter.calls == 0
    # A provider-side timeout reaches the stage as an unavailable provider.
    failure = ranking.stage_failure(TimeoutError())
    assert failure.reason == "timeout"
    assert ranking.stage_failure(providers.ProviderUnavailable("x")).reason == (
        "provider_unavailable"
    )
    assert ranking.stage_failure(ValueError("x")) is None
