"""Passage store for page revisions (migration 0037). Synthetic data only.

Pages come from a synthetic channel of record kind page_revision; no plugin
code is involved."""

import json
import math
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message
from psycopg import sql

from throughline import access, embeddings, passages
from throughline.db import connect

AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAGE = "fake.page"


# --- helpers ------------------------------------------------------------------


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text
    return r.json()


def page_channel(client, name=PAGE, **kwargs):
    return register(
        client, name, is_reference=True, record_kind="page_revision", **kwargs
    )


def page(i, external_id, content, at=None, channel=PAGE, thread=None, **kwargs):
    return message(
        i,
        channel=channel,
        external_id=external_id,
        thread={"external_id": thread or external_id},
        content=content,
        happened_at=(at or AT + timedelta(seconds=i)).isoformat(),
        fields={},
        **kwargs,
    )


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return r.json()


def build(limit=1000):
    with connect("app") as c:
        return passages.build(c, limit)


def message_id(external_id, version=None, channel=PAGE):
    with connect() as c:
        row = c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND (%s::int IS NULL OR m.version=%s::int) ORDER BY m.version DESC LIMIT 1",
            (channel, external_id, version, version),
        ).fetchone()
    return row["id"]


def cite(derived_id, source_id, kind="message", origin="source"):
    with connect() as c:
        c.execute(
            "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT %s,%s,m.id,m.version,%s FROM tl.messages m WHERE m.id=%s",
            (kind, derived_id, origin, source_id),
        )


def reader(statement, params=None, asker=None):
    with connect("reader") as c:
        if asker is not None:
            for name, value in (
                ("scope_mode", "asker"),
                ("asker", asker),
                ("unknown_asker", "deny"),
            ):
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        return c.execute(statement, params).fetchall()


def search(query, asker=None, **kwargs):
    names = list(kwargs)
    args = ",".join(f"{n}=>%s" for n in names)
    statement = (
        "SELECT * FROM api.search_passages(%s" + ("," + args if args else "") + ")"
    )
    return reader(statement, [query, *[kwargs[n] for n in names]], asker)


def passage_rows(where="", params=None, asker=None):
    return reader(
        "SELECT * FROM api.passages " + where + " ORDER BY passage_id", params, asker
    )


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


@pytest.fixture
def local_embedding(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "3")
    for name in (
        "THROUGHLINE_EMBEDDING_BASE_URL",
        "THROUGHLINE_EMBEDDING_MODEL",
        "THROUGHLINE_PASSAGE_EMBED_HISTORY",
    ):
        monkeypatch.delenv(name, raising=False)


MARKDOWN = """Preamble line about the synthetic orchard.

# Orchard notes

Intro paragraph of the page.

## Planting

{planting}

```
# not a heading inside a fence
```

## Harvest

Short harvest note.

### Apples

Short apple note.

# Second part

{long_sentence}
"""


def markdown():
    planting = "\n\n".join(
        " ".join(f"Synthetic planting sentence {p}-{s} about rows." for s in range(12))
        for p in range(8)
    )
    long_sentence = "word" + " lengthyword" * 400
    return MARKDOWN.format(planting=planting, long_sentence=long_sentence)


# --- BRN-6 splitting ------------------------------------------------------------


def test_brn_6_split_deterministic_offsets_slice_text():
    text = markdown()
    first = passages.split(text)
    assert first == passages.split(text)
    assert len(first) > 3
    assert [p["ordinal"] for p in first] == list(range(len(first)))
    previous_end = 0
    for p in first:
        assert p["content"] == text[p["char_start"] : p["char_end"]]
        assert p["content"].strip() and p["content"] == p["content"].strip()
        assert p["char_start"] >= previous_end
        previous_end = p["char_end"]
        assert p["text_hash"] == passages.text_hash(p["content"])
        assert p["token_estimate"] == passages.tokens(p["content"])
    # Every non-blank character of the text is in exactly one passage.
    covered = "".join(text[p["char_start"] : p["char_end"]] for p in first)
    assert "".join(covered.split()) == "".join(text.split())
    # Plain text without headings and odd whitespace.
    for sample in ("  single line  ", "a\r\n\r\nb", "x" * 5000, "\n\n# Only\n\n"):
        parts = passages.split(sample, max_tokens=50, min_tokens=5)
        assert parts and all(
            p["content"] == sample[p["char_start"] : p["char_end"]] for p in parts
        )
    assert passages.split("   \n ") == []
    with pytest.raises(ValueError):
        passages.split("x", max_tokens=0)


def test_brn_6_heading_paths_and_size_limits():
    text = markdown()
    parts = passages.split(text, max_tokens=120, min_tokens=40)
    paths = [p["heading_path"] for p in parts]
    assert all(p["token_estimate"] <= 120 for p in parts)
    # The preamble (small) merges with the first section, keeping the common
    # (empty) path.
    assert paths[0] == []
    assert parts[0]["content"].startswith("Preamble line")
    planting = [p for p in parts if p["heading_path"] == ["Orchard notes", "Planting"]]
    # Oversize section: split at paragraphs, each piece within the limit.
    assert len(planting) >= 3
    assert planting[0]["content"].startswith("## Planting")
    assert all("Synthetic planting sentence" in p["content"] for p in planting)
    # A heading inside a fenced block is text, not a section.
    assert not any("not a heading" in "/".join(p) for p in paths)
    assert any("# not a heading" in p["content"] for p in parts)
    # Small siblings and a small child are merged under the common path.
    harvest = [p for p in parts if "Short harvest note" in p["content"]]
    assert len(harvest) == 1 and "Short apple note" in harvest[0]["content"]
    assert harvest[0]["heading_path"] == ["Orchard notes", "Harvest"]
    # One sentence longer than the limit is cut at the hard limit.
    second = [p for p in parts if p["heading_path"] == ["Second part"]]
    assert len(second) > 1 and all(len(p["content"]) <= 480 for p in second)
    assert "".join("".join(p["content"].split()) for p in second).endswith(
        "lengthyword"
    )
    # Default limits keep a whole small page in one passage.
    small = passages.split("# T\n\nOne.\n\n## A\n\nTwo.")
    assert [(p["heading_path"], p["content"]) for p in small] == [
        (["T"], "# T\n\nOne.\n\n## A\n\nTwo.")
    ]


def test_full_text_paths():
    payload = {
        "compiled_truth": "",
        "body": "Body text",
        "timeline": ["first entry", {"date": "2020"}],
        "a/b": {"~k": "escaped"},
    }
    assert passages.full_text(payload, None, "content") == "content"
    assert passages.full_text(None, ["/body"], "content") == "content"
    assert passages.full_text(payload, ["/missing"], "content") == "content"
    assert (
        passages.full_text(payload, ["/compiled_truth|/body", "/timeline"], "c")
        == 'Body text\n\nfirst entry\n{"date": "2020"}'
    )
    assert passages.full_text(payload, ["/a~1b/~0k"], "c") == "escaped"
    assert passages.full_text(payload, ["/timeline/0"], "c") == "first entry"


# --- BRN-6 store ------------------------------------------------------------------


def test_brn_6_long_page_beyond_indexing_cut_reachable(client):
    page_channel(client, passage_text_paths=["/compiled_truth|/body"])
    body = (
        "# Long page\n\n"
        + "\n\n".join(f"Synthetic filler paragraph {i} " * 20 for i in range(700))
        + "\n\n## Ending\n\nThe quokkaharbor appears only here. "
        + "Closing synthetic words. " * 10
    )
    assert len(body.encode()) > 250_000
    cut = body[:1000]
    put(client, [page(0, "long", cut, payload={"compiled_truth": "", "body": body})])
    assert build() == 1
    assert search("quokkaharbor", max_results=5) != []
    hit = search("quokkaharbor")[0]
    row = passage_rows("WHERE passage_id=%s", (hit["passage_id"],))[0]
    assert "quokkaharbor" in row["content"] and "quokkaharbor" not in cut
    assert row["heading_path"] == ["Long page", "Ending"]
    assert body[row["char_start"] : row["char_end"]] == row["content"]
    assert row["char_start"] > 250_000
    # The message itself (cut content) does not contain the word.
    assert reader("SELECT * FROM api.search_all('quokkaharbor',5)") == []
    assert reader("SELECT count(*) n FROM api.passages_of(%s)", (hit["message_id"],))[
        0
    ]["n"] == len(passage_rows())


def test_brn_6_passage_carries_page_revision_and_source_refs(client):
    page_channel(client)
    register(client, "fake.life")
    put(
        client,
        [
            message(50, channel="fake.life", external_id="src", fields={}),
            message(51, channel="fake.life", external_id="src2", fields={}),
            page(0, "cited", "# Cited\n\nThe synthetic orchard page with sources."),
            page(1, "bare", "# Bare\n\nThe synthetic orchard page without sources."),
        ],
    )
    cited, bare = message_id("cited"), message_id("bare")
    source = message_id("src", channel="fake.life")
    cite(cited, source)
    assert build() == 2
    with connect("reader") as c:
        records = {
            r["external_id"]: r
            for r in c.execute("SELECT * FROM api.passage_records").fetchall()
        }
    record = records["cited"]
    passage = passage_rows("WHERE message_id=%s", (cited,))[0]
    assert record["record_id"] == f"p:{passage['passage_id']}" == passage["record_id"]
    assert record["kind"] == "passage" and record["ref_id"] == passage["passage_id"]
    assert record["message_id"] == cited and record["version"] == 1
    assert record["is_current"] and record["generated"]
    assert record["channel"] == PAGE and record["record_time"] == AT
    assert record["heading_path"] == ["Cited"]
    assert (record["char_start"], record["char_end"]) == (0, len(record["content"]))
    assert record["support"] == [
        {"message_id": source, "version": 1, "relation": "derived_from"}
    ]
    assert not record["unsupported"]
    assert record["expansion"] == {
        "page": [PAGE, "cited"],
        "revision": cited,
        "sources": True,
    }
    assert record["provenance"]["method"] == "passage"
    assert record["provenance"]["external_id"] == "cited"
    assert record["loc_reason"] == "not_applicable" and record["lat"] is None
    unsupported = records["bare"]
    assert unsupported["message_id"] == bare
    assert unsupported["support"] == [] and unsupported["unsupported"]
    assert unsupported["generated"]
    # Passage-level sources replace the revision's.
    other = message_id("src2", channel="fake.life")
    cite(record["ref_id"], other, kind="passage", origin="job")
    again = reader(
        "SELECT support FROM api.passage_records WHERE ref_id=%s", (record["ref_id"],)
    )[0]
    assert again["support"] == [
        {"message_id": other, "version": 1, "relation": "derived_from"}
    ]
    # Agent provenance is never support.
    cite(unsupported["ref_id"], other, kind="passage", origin="agent")
    assert reader(
        "SELECT unsupported FROM api.passage_records WHERE ref_id=%s",
        (unsupported["ref_id"],),
    )[0]["unsupported"]
    with connect() as c:
        assert c.execute(
            "SELECT relation FROM tl.record_sources WHERE prefix='p'"
        ).fetchone() == {"relation": "api.passage_records"}


# --- BRN-5 revisions ----------------------------------------------------------------


def revisions(client):
    """Three revisions of one page: alpha (day 0), beta (day 10), gamma (day 20)."""
    page_channel(client)
    for word, day in (("alpha", 0), ("beta", 10), ("gamma", 20)):
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
        for r in reader(
            "SELECT id,version FROM api.message_versions WHERE channel=%s", (PAGE,)
        )
    }


def revision_of(rows):
    ids = {r["message_id"] for r in rows}
    return ids


def test_brn_5_current_default_as_of_and_all_versions(client):
    ids = revisions(client)
    assert revision_of(search("orchard")) == {ids[3]}
    assert search("alpha") == [] and search("beta") == []
    assert revision_of(search("orchard", as_of=AT + timedelta(days=5))) == {ids[1]}
    assert revision_of(search("orchard", as_of=AT + timedelta(days=15))) == {ids[2]}
    assert revision_of(search("beta", as_of=AT + timedelta(days=15))) == {ids[2]}
    assert search("orchard", as_of=AT - timedelta(days=1)) == []
    assert revision_of(search("orchard", current_only=False)) == set(ids.values())
    assert revision_of(search("alpha", current_only=False)) == {ids[1]}
    with connect() as c:
        stored = {
            r["id"]: r["ingested_at"]
            for r in c.execute("SELECT id,ingested_at FROM tl.messages")
        }
    # known_by hides revisions stored later.
    assert revision_of(
        search("orchard", as_of=AT + timedelta(days=30), known_by=stored[ids[1]])
    ) == {ids[1]}
    assert revision_of(
        search("orchard", as_of=AT + timedelta(days=30), known_by=stored[ids[2]])
    ) == {ids[2]}
    current = {r["message_id"]: r["is_current"] for r in passage_rows()}
    assert current == {ids[1]: False, ids[2]: False, ids[3]: True}
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        search("orchard", time_basis="bogus")


def test_brn_5_authorized_revision_fallback_in_sql(client):
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
    first, current = message_id("wiki", 1), message_id("wiki", 2)
    public = message_id("pub", channel="fake.public")
    secret = message_id("sec", channel="fake.secret")
    cite(first, public)
    cite(current, public)
    cite(current, secret)
    assert build() == 2
    access.register_asker("guest")
    access.grant("guest", channel=PAGE)
    access.grant("guest", channel="fake.public")
    # Unrestricted: the current revision.
    assert revision_of(search("orchard")) == {current}
    # Restricted: the current revision cites a hidden message, so the latest
    # authorized revision answers.
    assert revision_of(search("orchard", asker="guest")) == {first}
    assert search("beta", asker="guest") == []
    assert search("beta", asker="guest", current_only=False) == []
    shown = passage_rows(asker="guest")
    assert {r["message_id"] for r in shown} == {first}
    assert passage_rows("WHERE message_id=%s", (current,), asker="guest") == []
    assert (
        reader(
            "SELECT * FROM api.passage_records WHERE message_id=%s", (current,), "guest"
        )
        == []
    )
    assert reader("SELECT * FROM api.passages_of(%s)", (current,), "guest") == []
    # Visible again once the source channel is granted.
    access.grant("guest", channel="fake.secret")
    assert revision_of(search("orchard", asker="guest")) == {current}
    # A hidden passage-level source hides that passage only.
    access.revoke("guest", channel="fake.secret")
    passage = passage_rows("WHERE message_id=%s", (first,))[0]["passage_id"]
    cite(passage, secret, kind="passage")
    assert passage_rows(asker="guest") == []
    assert search("orchard", asker="guest") == []


def test_acl_9_passage_views_have_no_oracle(client):
    page_channel(client)
    register(client, "fake.public")
    put(
        client,
        [
            message(50, channel="fake.public", external_id="pub", fields={}),
            page(0, "wiki", "Synthetic zebra secret page."),
        ],
    )
    assert build() == 1
    access.register_asker("guest")
    access.grant("guest", channel="fake.public")
    probes = [
        "SELECT * FROM api.passages WHERE 1/(CASE WHEN content LIKE '%zebra%' THEN 0 ELSE 1 END)=0",
        "SELECT * FROM api.passage_records WHERE 1/(CASE WHEN content LIKE '%zebra%' THEN 0 ELSE 1 END)=0",
        "SELECT * FROM api.passages WHERE (CASE WHEN content LIKE '%zebra%' THEN 'x' ELSE '1' END)::int=1",
        "SELECT * FROM api.search_passages('zebra',5) s WHERE 1/(CASE WHEN s.score>0 THEN 0 ELSE 1 END)=0",
    ]
    from throughline.query import validate_sql

    for statement in probes:
        # The probe is real: an unrestricted reader fails on the hidden row.
        with pytest.raises(psycopg.DataError):
            reader(statement)
        assert reader(validate_sql(statement), asker="guest") == [], statement
    for view in ("passages", "passage_records"):
        assert reader(f"SELECT count(*) n FROM api.{view}", asker="guest")[0]["n"] == 0
    assert search("zebra", asker="guest") == []


# --- QRY-20 scope before ranking ------------------------------------------------------


def test_qry_20_passage_scope_before_ranking(client):
    page_channel(client)
    register(client, "fake.life")
    put(client, [message(9000, channel="fake.life", external_id="source", fields={})])
    groups = dict(
        time=range(0, 5),
        thread=range(5, 10),
        terms=range(10, 15),
        sources=range(15, 20),
        exclude=range(20, 25),
        subject=range(25, 30),
    )
    items = []
    for i in range(500):
        special = i < 30
        words = f"synthetic orchard {i}" if special else "orchard " * 8 + f"filler {i}"
        if i in groups["terms"]:
            words += " zebra"
        extra = {}
        at = AT + timedelta(seconds=i)
        if i in groups["time"]:
            at = datetime(2021, 6, 1, tzinfo=timezone.utc) + timedelta(seconds=i)
        if i in groups["subject"]:
            extra["subject"] = dict(
                start="2015-03-01T00:00:00+00:00",
                end="2015-03-31T00:00:00+00:00",
                start_precision="day",
                end_precision="day",
            )
        if i == 28:
            # Unknown bounds are open.
            extra["subject"] = dict(
                start="2030-01-01T00:00:00+00:00",
                end="2030-02-01T00:00:00+00:00",
                start_precision="unknown",
                end_precision="day",
            )
        if i == 29:
            # A missing end is open.
            extra["subject"] = dict(start="2010-01-01T00:00:00+00:00")
        thread = "special" if i in groups["thread"] else None
        items.append(page(i, f"page-{i}", words, at, thread=thread, **extra))
    put(client, items)
    assert build() == 500
    ids = {
        r["external_id"]: r["id"]
        for r in reader(
            "SELECT id,external_id FROM api.messages WHERE channel=%s", (PAGE,)
        )
    }
    source = message_id("source", channel="fake.life")
    for i in groups["sources"]:
        cite(ids[f"page-{i}"], source)

    def expect(name):
        return {ids[f"page-{i}"] for i in groups[name]}

    unfiltered = search("orchard", max_results=5)
    assert len(unfiltered) == 5 and not revision_of(unfiltered) & set(
        ids[f"page-{i}"] for i in range(30)
    )
    june = datetime(2021, 6, 1, tzinfo=timezone.utc)
    assert revision_of(
        search(
            "orchard", max_results=5, after_at=june, before_at=june + timedelta(days=1)
        )
    ) == expect("time")
    thread_id = reader(
        "SELECT DISTINCT thread_id FROM api.passages WHERE external_id='page-5'"
    )[0]["thread_id"]
    assert revision_of(search("orchard", max_results=5, thread_ids=[thread_id])) == (
        expect("thread")
    )
    assert revision_of(
        search("orchard", max_results=5, must_terms=["zebra", "the"])
    ) == (expect("terms"))
    assert revision_of(search("orchard", max_results=5, source_ids=[source])) == (
        expect("sources")
    )
    keep = expect("exclude")
    assert (
        revision_of(
            search(
                "orchard",
                max_results=5,
                exclude_message_ids=[v for v in ids.values() if v not in keep],
            )
        )
        == keep
    )
    march = dict(
        after_at=datetime(2015, 3, 10, tzinfo=timezone.utc),
        before_at=datetime(2015, 3, 20, tzinfo=timezone.utc),
    )
    assert revision_of(
        search("orchard", max_results=5, time_basis="subject", **march)
    ) == expect("subject")
    assert search("orchard", max_results=5, time_basis="record", **march) == []
    assert revision_of(
        search("orchard", max_results=5, time_basis="either", **march)
    ) == expect("subject")
    # Without a subject interval the record time decides under 'subject'.
    assert revision_of(
        search(
            "orchard",
            max_results=10,
            time_basis="subject",
            after_at=june,
            before_at=june + timedelta(days=1),
        )
    ) == expect("time") | {ids["page-28"], ids["page-29"]}
    assert revision_of(
        search("orchard", max_results=5, message_ids=sorted(expect("time")))
    ) == expect("time")
    # Combined filters intersect.
    assert search("orchard", must_terms=["zebra"], thread_ids=[thread_id]) == []
    # An exhaustive call (NULL limit) returns every eligible passage.
    assert len(search("orchard", max_results=None)) == 500


# --- EMB-2 and the worker ---------------------------------------------------------------


def test_emb_2_identical_text_embedded_once_and_conversation_worker_skips_page_threads(
    client, channel, local_embedding, monkeypatch
):
    page_channel(client)
    shared = "# Shared\n\nThe synthetic orchard text shared by two pages."
    put(
        client,
        [
            message(0, content="Synthetic chat about the orchard"),
            page(1, "one", shared),
            page(2, "two", shared),
            page(3, "three", "# Other\n\nA different synthetic page."),
        ],
    )
    calls = []

    def fake(texts, model=None, dimensions=None):
        calls.append(list(texts))
        return [[1.0, float(len(t) % 3), 0.5] for t in texts], 5

    monkeypatch.setattr(embeddings, "embed", fake)
    count = embeddings.run_once()
    assert len(calls) == 2
    conversation_texts, passage_texts = calls
    assert len(conversation_texts) == 1 and "chat" in conversation_texts[0]
    assert sorted(passage_texts) == sorted(
        [shared, "# Other\n\nA different synthetic page."]
    )
    assert count == 1 + 3 + 2
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.passages").fetchone()["n"] == 3
        assert (
            c.execute("SELECT count(*) n FROM tl.passage_embeddings").fetchone()["n"]
            == 2
        )
        # Conversations of page threads exist but are never embedded.
        page_conversations = c.execute(
            "SELECT count(*) n FROM tl.conversations k JOIN tl.threads t ON t.id=k.thread_id JOIN tl.channels ch ON ch.id=t.channel_id WHERE ch.name=%s",
            (PAGE,),
        ).fetchone()["n"]
        assert page_conversations > 0
        assert c.execute("SELECT count(*) n FROM tl.embeddings").fetchone()["n"] == 1
        ledger = c.execute(
            "SELECT model,tokens FROM tl.embedding_ledger ORDER BY id"
        ).fetchall()
        active = c.execute(
            "SELECT model FROM tl.embedding_models WHERE active"
        ).fetchall()
    assert ledger == [dict(model="local-hash-v1", tokens=5)] * 2
    # Covered: conversations outside page channels and every distinct text.
    assert active == [dict(model="local-hash-v1")]
    assert embeddings.run_once() == 0 and len(calls) == 2
    with connect() as c:
        assert passages.pending(c) == dict(
            revisions_without_passages=0,
            texts_without_vector=0,
            model="local-hash-v1",
            history=False,
        )
    # A new revision: the model stays active until its passages are covered.
    put(client, [page(3, "three", "# Other\n\nA changed synthetic page.")])
    with connect() as c:
        assert passages.pending(c)["revisions_without_passages"] == 1
    assert embeddings.run_once() == 2
    assert calls[-1] == ["# Other\n\nA changed synthetic page."]
    # History is embedded only on request.
    first_four, second_four = (
        "# Four\n\nA first synthetic revision.",
        "# Four\n\nA second synthetic revision.",
    )
    put(client, [page(4, "four", first_four)])
    put(client, [page(4, "four", second_four)])
    assert embeddings.run_once() == 2 + 1
    assert calls[-1] == [second_four]
    monkeypatch.setenv("THROUGHLINE_PASSAGE_EMBED_HISTORY", "true")
    with connect() as c:
        assert passages.pending(c)["texts_without_vector"] == 1
    assert embeddings.run_once() == 1
    assert calls[-1] == [first_four]
    with connect() as c:
        assert passages.pending(c)["texts_without_vector"] == 0


def test_vector_passages_exact(client):
    page_channel(client)
    texts = {
        "a": "Synthetic orchard page a.",
        "b": "Synthetic orchard page b.",
        "c": "Synthetic orchard page c.",
        "d": "Synthetic orchard page d.",
    }
    put(client, [page(i, k, v) for i, (k, v) in enumerate(texts.items())])
    assert build() == 4
    vectors = {"a": [1, 0, 0], "b": [0.6, 0.8, 0], "c": [0, 0, 1], "d": [1, 1]}
    other = {"a": [0, 0, 1], "b": [0, 1, 0], "c": [1, 0, 0]}
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_models(model,dimensions,active) VALUES('synthetic-vec',3,true),('synthetic-other',3,false)"
        )
        for key, text in texts.items():
            h = passages.text_hash(text)
            c.execute(
                "INSERT INTO tl.passage_embeddings(text_hash,model,embedding) VALUES(%s,'synthetic-vec',%s::vector)",
                (h, embeddings.vector(vectors[key])),
            )
            if key in other:
                c.execute(
                    "INSERT INTO tl.passage_embeddings(text_hash,model,embedding) VALUES(%s,'synthetic-other',%s::vector)",
                    (h, embeddings.vector(other[key])),
                )
    ids = {r["external_id"]: r["message_id"] for r in passage_rows()}
    query = [0.8, 0.6, 0.0]

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b)) / (
            math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        )

    rows = reader(
        "SELECT * FROM api.vector_passages(%s,10)", (embeddings.vector(query),)
    )
    # The 2-dimensional vector is skipped, never an error.
    expected = sorted(
        ((k, cosine(query, v)) for k, v in vectors.items() if len(v) == 3),
        key=lambda kv: -kv[1],
    )
    assert [r["message_id"] for r in rows] == [ids[k] for k, _ in expected]
    for row, (_, score) in zip(rows, expected):
        assert row["score"] == pytest.approx(score, abs=1e-6)
    named = reader(
        "SELECT * FROM api.vector_passages(%s,10,serving_model=>'synthetic-other')",
        (embeddings.vector([1, 0, 0]),),
    )
    assert [r["message_id"] for r in named][0] == ids["c"]
    assert len(named) == 3
    # Filters apply before the limit.
    only = reader(
        "SELECT * FROM api.vector_passages(%s,1,message_ids=>%s)",
        (embeddings.vector(query), [ids["c"]]),
    )
    assert [r["message_id"] for r in only] == [ids["c"]]
    access.register_asker("guest")
    assert (
        reader(
            "SELECT * FROM api.vector_passages(%s,10)",
            (embeddings.vector(query),),
            asker="guest",
        )
        == []
    )


def test_embed_goes_through_module_attribute(client, local_embedding, monkeypatch):
    from throughline.providers import local

    def refuse(self, *args, **kwargs):
        raise AssertionError("the provider must not be called directly")

    monkeypatch.setattr(local.LocalHashEmbedder, "embed", refuse)
    page_channel(client)
    put(client, [page(0, "one", "# One\n\nSynthetic page one.")])
    assert build() == 1
    seen = []

    def fake(texts, model=None, dimensions=None):
        seen.append(list(texts))
        return [[0.0, 1.0, 0.0] for _ in texts], 3

    monkeypatch.setattr(embeddings, "embed", fake)
    assert passages.embed_once() == 1
    assert seen == [["# One\n\nSynthetic page one."]]
    assert passages.embed_once() == 0 and len(seen) == 1
    with connect() as c:
        assert c.execute(
            "SELECT embedding::text e FROM tl.passage_embeddings"
        ).fetchone() == dict(e="[0,1,0]")
    # A configuration error returns 0 without a call.
    monkeypatch.delenv("THROUGHLINE_EMBEDDING_PROVIDER")
    assert passages.embed_once() == 0 and len(seen) == 1


def test_existing_fake_embed_tests_make_no_passage_calls(
    client, channel, local_embedding, monkeypatch
):
    put(client, [message(0), message(4000, content="ocean")])
    seen = []

    def fake(texts, model=None, dimensions=None):
        seen.append(list(texts))
        return [[0.0, 1.0, 0.0] for _ in texts], 11

    monkeypatch.setattr(embeddings, "embed", fake)
    assert embeddings.run_once() == 2
    assert len(seen) == 1 and len(seen[0]) == 2
    assert embeddings.run_once() == 0 and len(seen) == 1
    assert passages.embed_once() == 0 and len(seen) == 1
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.passages").fetchone()["n"] == 0
        assert c.execute(
            "SELECT model FROM tl.embedding_models WHERE active"
        ).fetchone() == dict(model="local-hash-v1")
        assert passages.pending(c)["revisions_without_passages"] == 0


# --- contract, concurrency, purge, registry -----------------------------------------------


def test_passage_records_contract_columns_match_record_versions(client):
    def columns(view):
        with connect() as c:
            return [
                (r["column_name"], r["data_type"], r["udt_name"])
                for r in c.execute(
                    "SELECT column_name,data_type,udt_name FROM information_schema.columns WHERE table_schema='api' AND table_name=%s ORDER BY ordinal_position",
                    (view,),
                )
            ]

    assert columns("passage_records") == columns("record_versions")
    with connect("reader") as c:
        cursor = c.execute("SELECT * FROM api.passage_records LIMIT 0")
        names = [d.name for d in cursor.description]
        assert names == [n for n, _, _ in columns("record_versions")]
        assert [
            d.name for d in c.execute("SELECT * FROM api.passages LIMIT 0").description
        ][:16] == [
            "passage_id",
            "record_id",
            "message_id",
            "channel_id",
            "channel",
            "external_id",
            "version",
            "is_current",
            "ordinal",
            "heading_path",
            "char_start",
            "char_end",
            "content",
            "token_estimate",
            "revision_time",
            "thread_id",
        ]
    with connect() as c:
        gates = {
            r["view_name"]
            for r in c.execute("SELECT view_name FROM tl.gates").fetchall()
        }
    assert {"passages", "passage_records"} <= gates
    # Gated views are read-only for every role.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect("app") as c:
            c.execute("DELETE FROM api.passages")


def test_build_idempotent_concurrent(client):
    page_channel(client)
    put(
        client,
        [
            page(i, f"p{i}", markdown() if i % 3 == 0 else f"# P{i}\n\nShort page {i}.")
            for i in range(30)
        ],
    )
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        try:
            with connect("app") as c:
                barrier.wait()
                results.append(passages.build(c, 1000))
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and sum(results) == 30
    expected = sum(
        len(passages.split(markdown() if i % 3 == 0 else f"# P{i}\n\nShort page {i}."))
        for i in range(30)
    )
    with connect() as c:
        stored = c.execute(
            "SELECT count(*) n, count(DISTINCT (message_id,ordinal)) d FROM tl.passages"
        ).fetchone()
    assert stored == dict(n=expected, d=expected)
    assert build() == 0
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM tl.passages").fetchone()["n"] == expected
        )
    # The stored rows are the split of the revision's text.
    first = message_id("p0")
    rows = reader("SELECT * FROM api.passages_of(%s)", (first,))
    assert [
        (r["ordinal"], r["heading_path"], r["char_start"], r["char_end"], r["content"])
        for r in rows
    ] == [
        (p["ordinal"], p["heading_path"], p["char_start"], p["char_end"], p["content"])
        for p in passages.split(markdown())
    ]


def test_purge_hook(client):
    page_channel(client)
    page_channel(client, "fake.other")
    register(client, "fake.life")
    shared = "Synthetic shared orchard text."
    put(
        client,
        [
            message(50, channel="fake.life", external_id="src", fields={}),
            page(0, "a", shared),
            page(1, "b", "Synthetic text only on the purged page."),
            page(2, "c", shared, channel="fake.other"),
        ],
    )
    assert build() == 3
    with connect() as c:
        for text in (shared, "Synthetic text only on the purged page."):
            c.execute(
                "INSERT INTO tl.passage_embeddings(text_hash,model,embedding) VALUES(%s,'synthetic-vec','[1,0,0]')",
                (passages.text_hash(text),),
            )
    purged = passage_rows("WHERE channel=%s", (PAGE,))
    cite(
        purged[0]["passage_id"], message_id("src", channel="fake.life"), kind="passage"
    )
    with connect() as c:
        hooks = {
            r["name"]: r["ord"]
            for r in c.execute("SELECT name,ord FROM tl.purge_hooks")
        }
        assert "passages" in hooks
        cid = c.execute("SELECT id FROM tl.channels WHERE name=%s", (PAGE,)).fetchone()[
            "id"
        ]
        c.execute("SET LOCAL throughline.purge='on'")
        c.execute("SELECT tl.run_purge_hooks(%s)", (cid,))
    with connect() as c:
        left = c.execute("SELECT channel_id FROM tl.passages").fetchall()
        hashes = {
            r["text_hash"]
            for r in c.execute("SELECT text_hash FROM tl.passage_embeddings")
        }
        lineage = c.execute(
            "SELECT count(*) n FROM tl.provenance WHERE derived_kind='passage'"
        ).fetchone()["n"]
    with connect() as c:
        kept = c.execute(
            "SELECT id FROM tl.channels WHERE name='fake.other'"
        ).fetchone()["id"]
    assert {r["channel_id"] for r in left} == {kept}
    assert hashes == {passages.text_hash(shared)}
    assert lineage == 0


def test_registry_examples_execute(client):
    from throughline import allowlist
    from throughline.query import validate_sql

    with connect() as c:
        registry = {
            r["name"]: r
            for r in c.execute(
                "SELECT * FROM tl.api_functions WHERE name IN ('search_passages','vector_passages','passages_of')"
            ).fetchall()
        }
        section = c.execute(
            "SELECT body FROM tl.catalog_sections WHERE subject='passages'"
        ).fetchone()
    assert set(registry) == {"search_passages", "vector_passages", "passages_of"}
    assert {
        n: (r["ranked_limit_arg"], r["default_limit"]) for n, r in registry.items()
    } == {
        "search_passages": (2, 20),
        "vector_passages": (2, 20),
        "passages_of": (None, None),
    }
    # Beyond throughline.ann_exact_limit the vector ranking uses an index.
    assert {n: r["exactness"] for n, r in registry.items()} == {
        "search_passages": "exact",
        "vector_passages": "approximate",
        "passages_of": "exact",
    }
    assert "max_results integer DEFAULT 20" in registry["search_passages"]["signature"]
    assert "serving_model text DEFAULT NULL" in registry["vector_passages"]["signature"]
    functions, scalar = allowlist.load()
    assert {"search_passages", "vector_passages", "passages_of"} <= functions
    for name, row in registry.items():
        assert row["signature"].startswith(name + "(")
        reader(validate_sql(row["example"]))
    assert section and "full_text" in section["body"]
    catalog = reader("SELECT api.catalog() c")[0]["c"]
    assert "passages" in catalog["sections"]
    assert {"search_passages", "vector_passages", "passages_of"} <= {
        f["name"] for f in catalog["functions"]
    }
    assert reader("SELECT api.describe('passages') d")[0]["d"] == section["body"]


def test_lib_passages_commands(client):
    import typer
    from typer.testing import CliRunner

    from throughline.cli_commands.passages import register as register_commands

    page_channel(client)
    put(client, [page(0, "one", "# One\n\nSynthetic page one.")])
    group = typer.Typer()
    register_commands(group)

    @group.command()
    def other():
        pass

    runner = CliRunner()
    result = runner.invoke(group, ["passages", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["revisions_without_passages"] == 1
    result = runner.invoke(group, ["passages", "build", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["built"] == 1
    status = json.loads(runner.invoke(group, ["passages", "status", "--json"]).output)
    assert status["revisions_without_passages"] == 0 and status["passages"] == 1


# --- checker additions ----------------------------------------------------------------


def test_build_skips_revision_locked_by_another_run(client, monkeypatch):
    """A revision being split by another run is left to it: two runs (even with
    different limits) never interleave the ordinals of one revision."""
    page_channel(client)
    put(client, [page(0, "one", markdown())])
    revision = message_id("one")
    with connect("app") as holder:
        assert holder.execute(
            "SELECT tl.try_lock_passage_build(%s) ok", (revision,)
        ).fetchone()["ok"]
        monkeypatch.setenv("THROUGHLINE_PASSAGE_MAX_TOKENS", "60")
        monkeypatch.setenv("THROUGHLINE_PASSAGE_MIN_TOKENS", "5")
        assert build() == 0
        with connect() as c:
            assert c.execute("SELECT count(*) n FROM tl.passages").fetchone()["n"] == 0
        holder.rollback()
    monkeypatch.delenv("THROUGHLINE_PASSAGE_MAX_TOKENS")
    monkeypatch.delenv("THROUGHLINE_PASSAGE_MIN_TOKENS")
    assert build() == 1
    # Another limit later changes nothing: the revision is built.
    monkeypatch.setenv("THROUGHLINE_PASSAGE_MAX_TOKENS", "60")
    monkeypatch.setenv("THROUGHLINE_PASSAGE_MIN_TOKENS", "5")
    assert build() == 0
    rows = reader("SELECT * FROM api.passages_of(%s)", (revision,))
    assert [r["content"] for r in rows] == [
        p["content"] for p in passages.split(markdown())
    ]


def test_vector_passages_authorized_revision_fallback(client):
    """The vector form decides revisions among those the asker may read, like
    the keyword form."""
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
    old, new = "Synthetic orchard alpha revision.", "Synthetic orchard beta revision."
    put(client, [page(0, "wiki", old, AT)])
    put(client, [page(0, "wiki", new, AT + timedelta(days=1))])
    first, current = message_id("wiki", 1), message_id("wiki", 2)
    public = message_id("pub", channel="fake.public")
    secret = message_id("sec", channel="fake.secret")
    cite(first, public)
    cite(current, secret)
    assert build() == 2
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_models(model,dimensions,active) VALUES('synthetic-vec',3,true)"
        )
        for text, vec in ((old, [1, 0, 0]), (new, [0, 1, 0])):
            c.execute(
                "INSERT INTO tl.passage_embeddings(text_hash,model,embedding) VALUES(%s,'synthetic-vec',%s::vector)",
                (passages.text_hash(text), embeddings.vector(vec)),
            )
    query = embeddings.vector([0, 1, 0])
    statement = "SELECT * FROM api.vector_passages(%s,10)"
    assert revision_of(reader(statement, (query,))) == {current}
    access.register_asker("guest")
    access.grant("guest", channel=PAGE)
    access.grant("guest", channel="fake.public")
    assert revision_of(reader(statement, (query,), "guest")) == {first}
    every = reader(
        "SELECT * FROM api.vector_passages(%s,10,current_only=>false)",
        (query,),
        "guest",
    )
    assert revision_of(every) == {first}


def test_known_by_without_as_of_and_passage_sources_scope(client):
    ids = revisions(client)
    with connect() as c:
        stored = {
            r["id"]: r["ingested_at"]
            for r in c.execute("SELECT id,ingested_at FROM tl.messages")
        }
    # known_by alone: the latest revision stored by then.
    assert revision_of(search("orchard", known_by=stored[ids[2]])) == {ids[2]}
    register(client, "fake.life")
    put(
        client,
        [
            message(60, channel="fake.life", external_id="s1", fields={}),
            message(61, channel="fake.life", external_id="s2", fields={}),
        ],
    )
    s1, s2 = (message_id(k, channel="fake.life") for k in ("s1", "s2"))
    cite(ids[3], s1)
    assert revision_of(search("orchard", source_ids=[s1])) == {ids[3]}
    assert search("orchard", source_ids=[s2]) == []
    # Passage-level sources replace the revision's for the source scope.
    passage = passage_rows("WHERE message_id=%s", (ids[3],))[0]["passage_id"]
    cite(passage, s2, kind="passage")
    assert search("orchard", source_ids=[s1]) == []
    assert revision_of(search("orchard", source_ids=[s2])) == {ids[3]}
