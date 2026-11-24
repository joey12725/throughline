"""Semantic operator expand: scoped, set-based expansion of result sets
(migration 0041, QRY-32, QRY-20, QRY-27, ACL-8). Synthetic data only."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message
from psycopg.types.json import Jsonb

from throughline import passages, resultsets
from throughline.db import connect
from throughline.retrieval import expand as expand_module
from throughline.retrieval.expand import ExpandUnavailable, expand
from throughline.retrieval.types import RECORD_KEYS, HardScope
from throughline.scope import Scope

OWNER = Scope.local()
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
GUEST = "fake-guest"


@pytest.fixture(autouse=True)
def no_askers(database, monkeypatch):
    for name in (
        "THROUGHLINE_EXPAND_MAX_INPUT",
        "THROUGHLINE_EXPAND_MAX_OUTPUT",
        "THROUGHLINE_EXPAND_THREAD_CAP",
    ):
        monkeypatch.delenv(name, raising=False)
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


# --- helpers ---------------------------------------------------------------


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


def ids(channel="synthetic.chat"):
    rows = owner(
        "SELECT m.id,m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id",
        (channel,),
    )
    return {r["external_id"]: r["id"] for r in rows}


def thread(client, n, channel="synthetic.chat", thread_id="fake-thread", **kwargs):
    fields = {} if channel != "synthetic.chat" else None
    items = []
    for i in range(n):
        extra = dict(kwargs)
        if fields is not None:
            extra["fields"] = fields
        items.append(
            message(
                i,
                channel=channel,
                external_id=f"{thread_id}-{i}",
                thread={"external_id": thread_id},
                **extra,
            )
        )
    put(client, items)
    found = ids(channel)
    return [found[f"{thread_id}-{i}"] for i in range(n)]


def make(scope, records, completeness="exhaustive", **kwargs):
    return resultsets.create(scope, records, completeness=completeness, **kwargs)


def refs(message_ids, **extra):
    return [dict(record_id=f"m:{i}", **extra) for i in message_ids]


def read(scope, handle):
    return resultsets.read(scope, handle)


def added(scope, handle):
    return [r for r in read(scope, handle)["records"] if r["role"] != "hit"]


def added_ids(scope, handle):
    return [r["record_id"] for r in added(scope, handle)]


def link(kind, from_kind, from_id, to_kind, to_id, evidence=None):
    return owner(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence,method) VALUES (%s,%s,%s,%s,%s,%s,'owner') RETURNING id",
        (kind, from_kind, from_id, to_kind, to_id, Jsonb(evidence or {})),
    )[0]["id"]


def entity(kind, name, metadata=None):
    return owner(
        "INSERT INTO tl.entities(kind,name,metadata) VALUES (%s,%s,%s) RETURNING id",
        (kind, name, Jsonb(metadata or {})),
    )[0]["id"]


def cite(derived_kind, derived_id, source_id):
    owner(
        "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT %s,%s,m.id,m.version,'source' FROM tl.messages m WHERE m.id=%s",
        (derived_kind, derived_id, source_id),
    )


def restricted(*channels):
    owner("INSERT INTO tl.askers(name) VALUES (%s) ON CONFLICT DO NOTHING", (GUEST,))
    for channel in channels:
        owner(
            "INSERT INTO tl.asker_grants(asker,channel_id) SELECT %s,id FROM tl.channels WHERE name=%s",
            (GUEST, channel),
        )
    return Scope(
        token_id=None,
        token_kind="agent",
        asker=GUEST,
        mode="asker",
        unknown_asker="deny",
    )


class Statements:
    """Counts statements sent to the database (every role)."""

    def __init__(self, monkeypatch):
        self.n = 0
        for name in ("execute", "executemany", "stream"):
            original = getattr(psycopg.Cursor, name)
            monkeypatch.setattr(psycopg.Cursor, name, self._wrap(original))

    def _wrap(self, original):
        def wrapper(cursor, *args, **kwargs):
            query = str(args[0] if args else kwargs.get("query")).strip()
            # The connection pool pings a connection idle for longer than
            # db._IDLE_CHECK_S with exactly 'SELECT 1' before reuse (on a slow
            # shared machine, between two calls); that is not a statement of
            # the operation being measured.
            if not query.upper().startswith("SET") and query != "SELECT 1":
                self.n += 1
            return original(cursor, *args, **kwargs)

        return wrapper


# --- QRY-32 context ----------------------------------------------------------


def test_qry_32_context_exact_and_set_based(client, channel, monkeypatch):
    m = thread(client, 401)
    few = make(OWNER, refs(m[0:4:2]))
    many = make(OWNER, refs(m[0:400:2]))
    assert len(resultsets.members(OWNER, many)) == 200

    counter = Statements(monkeypatch)
    small = expand(OWNER, handle=few, mode="context", before=0, after=1)
    small_count, counter.n = counter.n, 0
    large = expand(OWNER, handle=many, mode="context", before=0, after=1)
    # Set-based: the number of statements does not grow with the anchors.
    assert counter.n <= small_count
    monkeypatch.undo()

    records = read(OWNER, large)["records"]
    hits = [r for r in records if r["role"] == "hit"]
    context = [r for r in records if r["role"] == "context"]
    assert [r["record_id"] for r in hits] == [f"m:{i}" for i in m[0:400:2]]
    # Exact: every odd neighbour, once, attached to its anchor.
    assert sorted(r["record_id"] for r in context) == sorted(
        f"m:{i}" for i in m[1:400:2]
    )
    for record in context:
        position = m.index(record["message_id"])
        assert record["depends_on"] == f"m:{m[position - 1]}"
        assert record["discovery"] == [
            dict(leg="expand:context", detail=dict(anchor=record["depends_on"]))
        ]
    assert resultsets.info(OWNER, large)["completeness"] == "exhaustive"
    assert added_ids(OWNER, small) == [f"m:{m[1]}", f"m:{m[3]}"]


def test_qry_32_context_is_span_aware(client, channel):
    m = thread(client, 12)
    handle = make(
        OWNER, [dict(record_id=f"m:{m[4]}", span=dict(first_no=5, last_no=7))]
    )
    out = expand(OWNER, handle=handle, mode="context", before=1, after=1)
    numbers = {r["record_id"]: r["thread_no"] for r in read(OWNER, out)["records"]}
    assert sorted(numbers.values()) == [4, 5, 6, 7, 8]
    whole = expand(OWNER, handle=handle, mode="thread")
    assert len(read(OWNER, whole)["records"]) == 12


# --- QRY-32 sources ----------------------------------------------------------


def test_qry_32_sources_from_passage_and_page(client, channel):
    register(client, "synthetic.pages", is_reference=True, record_kind="page_revision")
    m = thread(client, 3)
    put(
        client,
        [
            message(
                10,
                channel="synthetic.pages",
                external_id="fake-page",
                thread={"external_id": "fake-page"},
                content="# Synthetic heading\n\nSynthetic page text about orchard rows.",
                fields={},
            ),
            message(
                11,
                channel="synthetic.pages",
                external_id="fake-page-2",
                thread={"external_id": "fake-page-2"},
                content="# Other\n\nSynthetic second page about pears.",
                fields={},
            ),
        ],
    )
    pages = ids("synthetic.pages")
    page, other = pages["fake-page"], pages["fake-page-2"]
    cite("message", page, m[0])
    cite("message", other, m[1])
    with connect("app") as c:
        passages.build(c, 100)
    passage = owner(
        "SELECT id FROM tl.passages WHERE message_id=%s ORDER BY ordinal LIMIT 1",
        (other,),
    )[0]["id"]
    handle = make(
        OWNER,
        [
            dict(record_id=f"m:{page}"),
            dict(record_id=f"p:{passage}"),
            dict(record_id=f"m:{m[2]}"),
        ],
    )
    out = expand(OWNER, handle=handle, mode="sources")
    by_id = {r["record_id"]: r for r in added(OWNER, out)}
    assert by_id[f"m:{m[0]}"]["depends_on"] == f"m:{page}"
    assert by_id[f"m:{m[1]}"]["depends_on"] == f"p:{passage}"
    # A raw message is its own source: never added twice.
    assert f"m:{m[2]}" not in by_id
    assert set(by_id) == {f"m:{m[0]}", f"m:{m[1]}"}
    alone = expand(OWNER, handle=handle, mode="sources", keep_input=False)
    assert {r["record_id"] for r in read(OWNER, alone)["records"]} == {
        f"m:{m[0]}",
        f"m:{m[1]}",
        f"m:{m[2]}",
    }


# --- QRY-32 links, time, place -----------------------------------------------


def test_qry_32_links_time_place_modes(client, channel):
    base = AT
    put(
        client,
        [
            message(
                0,
                external_id="fake-a",
                thread={"external_id": "t-a"},
                happened_at=base.isoformat(),
                ended_at=(base + timedelta(hours=2)).isoformat(),
                location=dict(lat=10.0, lon=20.0),
            ),
            message(
                1,
                external_id="fake-b",
                thread={"external_id": "t-b"},
                happened_at=(base + timedelta(days=5)).isoformat(),
            ),
            message(
                2,
                external_id="fake-during",
                thread={"external_id": "t-c"},
                happened_at=(base + timedelta(hours=1)).isoformat(),
            ),
            message(
                3,
                external_id="fake-near",
                thread={"external_id": "t-d"},
                happened_at=(base + timedelta(days=9)).isoformat(),
                location=dict(lat=10.0001, lon=20.0001),
            ),
            message(
                4,
                external_id="fake-far",
                thread={"external_id": "t-e"},
                happened_at=(base + timedelta(days=20)).isoformat(),
                location=dict(lat=11.0, lon=21.0),
            ),
        ],
    )
    found = ids()
    a, b = found["fake-a"], found["fake-b"]
    farm = entity("concept", "Synthetic Willow Farm")
    about = link("about", "message", a, "entity", farm)
    reply = link("reply_to", "message", a, "message", b)
    handle = make(OWNER, refs([a]))

    links = expand(OWNER, handle=handle, mode="links")
    got = {r["record_id"]: r for r in added(OWNER, links)}
    assert set(got) == {f"m:{b}", f"e:{farm}", f"l:{about}", f"l:{reply}"}
    assert got[f"e:{farm}"]["kind"] == "entity"
    assert got[f"l:{reply}"]["kind"] == "link"
    assert all(r["depends_on"] == f"m:{a}" for r in got.values())
    typed = expand(OWNER, handle=handle, mode="links", link_type="reply_to")
    assert set(added_ids(OWNER, typed)) == {f"m:{b}", f"l:{reply}"}
    # From the entity record: the message linked to it comes back.
    from_entity = expand(OWNER, ids=[f"e:{farm}"], mode="links")
    assert f"m:{a}" in added_ids(OWNER, from_entity)

    during = expand(OWNER, handle=handle, mode="time")
    assert added_ids(OWNER, during) == [f"m:{found['fake-during']}"]
    wide = expand(OWNER, handle=handle, mode="time", window_s=6 * 86400)
    assert set(added_ids(OWNER, wide)) == {f"m:{found['fake-during']}", f"m:{b}"}

    near = expand(OWNER, handle=handle, mode="place", meters=100)
    assert added_ids(OWNER, near) == [f"m:{found['fake-near']}"]
    far = expand(OWNER, handle=handle, mode="place", meters=500000)
    assert set(added_ids(OWNER, far)) == {
        f"m:{found['fake-near']}",
        f"m:{found['fake-far']}",
    }
    for out in (links, during, near):
        assert resultsets.info(OWNER, out)["completeness"] == "exhaustive"


# --- QRY-32 focus ------------------------------------------------------------


def test_qry_32_focus_approximate(client, channel):
    words = [
        "Synthetic note about weather",
        "Synthetic apricot harvest plan",
        "Synthetic anchor message",
        "Synthetic unrelated remark",
        "Synthetic apricot jam recipe",
    ]
    put(
        client,
        [
            message(i, external_id=f"fake-{i}", content=text)
            for i, text in enumerate(words)
        ],
    )
    found = ids()
    anchor = found["fake-2"]
    handle = make(OWNER, refs([anchor]))
    out = expand(
        OWNER, handle=handle, mode="focus", focus="apricot", before=2, after=2, k=5
    )
    got = added(OWNER, out)
    assert {r["record_id"] for r in got} == {
        f"m:{found['fake-1']}",
        f"m:{found['fake-4']}",
    }
    assert all(r["depends_on"] == f"m:{anchor}" for r in got)
    assert all(r["discovery"][0]["leg"] == "expand:focus" for r in got)
    assert resultsets.info(OWNER, out)["completeness"] == "ranked_sample"
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, handle=handle, mode="focus")


# --- roles -------------------------------------------------------------------


def test_qry_32_roles_never_hit_and_depends_on(client, channel):
    m = thread(client, 6)
    target = entity("concept", "Synthetic Grove")
    link("about", "message", m[2], "entity", target)
    handle = make(OWNER, refs([m[2]]))
    for mode in ("context", "thread", "links", "time"):
        out = expand(OWNER, handle=handle, mode=mode, window_s=10)
        members = resultsets.members(OWNER, out)
        new = [x for x in members if x["record_id"] != f"m:{m[2]}"]
        assert new, mode
        assert all(x["role"] == "context" for x in new), mode
        assert all(x["depends_on"] == f"m:{m[2]}" for x in new), mode
        assert [x["role"] for x in members if x["record_id"] == f"m:{m[2]}"] == ["hit"]
    dependency = expand(OWNER, handle=handle, mode="context", role="dependency")
    assert {
        x["role"] for x in resultsets.members(OWNER, dependency) if x["role"] != "hit"
    } == {"dependency"}
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, handle=handle, mode="context", role="hit")
    records = read(OWNER, dependency)["records"]
    assert all(set(RECORD_KEYS) <= set(r) for r in records)


# --- as_of -------------------------------------------------------------------


def test_qry_32_as_of_inherited_and_links_asof(client, channel):
    m = thread(client, 3)
    cutoff = owner("SELECT clock_timestamp() AS t")[0]["t"]
    old_neighbour = m[1]
    # Edit the neighbour after the cutoff: a new version, ingested later.
    put(
        client,
        [
            message(
                1,
                external_id="fake-thread-1",
                thread={"external_id": "fake-thread"},
                content="Synthetic edited neighbour",
            )
        ],
    )
    current = ids()["fake-thread-1"]
    assert current != old_neighbour
    late = link("reply_to", "message", m[0], "message", m[2])

    historical = make(OWNER, refs([m[0]]), known_by=cutoff)
    out = expand(OWNER, handle=historical, mode="context", before=0, after=1)
    assert added_ids(OWNER, out) == [f"m:{old_neighbour}"]
    assert resultsets.info(OWNER, out)["known_by"] == cutoff
    now = expand(OWNER, handle=make(OWNER, refs([m[0]])), mode="context", after=1)
    assert added_ids(OWNER, now) == [f"m:{current}"]
    # A link created after the cutoff is not followed as of the cutoff.
    assert added_ids(OWNER, expand(OWNER, handle=historical, mode="links")) == []
    assert set(
        added_ids(OWNER, expand(OWNER, handle=make(OWNER, refs([m[0]])), mode="links"))
    ) == {f"m:{m[2]}", f"l:{late}"}
    # The caller's own cutoff wins over the set's.
    later = expand(
        OWNER,
        handle=historical,
        mode="context",
        before=0,
        after=1,
        known_by=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert added_ids(OWNER, later) == [f"m:{current}"]


# --- QRY-20 scope ------------------------------------------------------------


def test_qry_20_expand_within_scope_and_reduced_filter(client, channel):
    register(client, "fake.reference", is_reference=True)
    put(
        client,
        [
            message(0, external_id="fake-0", thread={"external_id": "mixed"}),
            message(1, external_id="fake-1", thread={"external_id": "mixed"}),
            message(2, external_id="fake-2", thread={"external_id": "mixed"}),
            message(
                3,
                channel="fake.reference",
                external_id="ref-0",
                thread={"external_id": "ref"},
                fields={},
            ),
            message(
                4,
                channel="fake.reference",
                external_id="ref-1",
                thread={"external_id": "ref"},
                fields={},
            ),
            message(
                5,
                external_id="fake-late",
                thread={"external_id": "mixed"},
                happened_at=(AT + timedelta(days=400)).isoformat(),
            ),
        ],
    )
    found = ids()
    refs_found = ids("fake.reference")
    handle = make(OWNER, refs([found["fake-1"], refs_found["ref-0"]]))
    # Reduced filter: the input's own kinds are admitted, so reference
    # context of a reference anchor is kept.
    out = expand(OWNER, handle=handle, mode="thread")
    assert set(added_ids(OWNER, out)) == {
        f"m:{found['fake-0']}",
        f"m:{found['fake-2']}",
        f"m:{found['fake-late']}",
        f"m:{refs_found['ref-1']}",
    }
    # within binds: kinds, time and as_of narrow what may be added.
    narrow = expand(
        OWNER,
        handle=handle,
        mode="thread",
        within=HardScope(kinds=["life_event"], before=AT + timedelta(days=1)),
    )
    assert set(added_ids(OWNER, narrow)) == {
        f"m:{found['fake-0']}",
        f"m:{found['fake-2']}",
    }
    as_of = expand(
        OWNER,
        handle=handle,
        mode="thread",
        within=HardScope(as_of=AT + timedelta(days=1)),
    )
    assert f"m:{found['fake-late']}" not in added_ids(OWNER, as_of)
    only = expand(
        OWNER,
        handle=handle,
        mode="thread",
        within=HardScope(message_ids=[found["fake-2"]]),
    )
    assert added_ids(OWNER, only) == [f"m:{found['fake-2']}"]
    entities = HardScope(kinds=["life_event"])
    farm = entity("concept", "Synthetic Farm")
    link("about", "message", found["fake-1"], "entity", farm)
    linked = expand(OWNER, handle=handle, mode="links", within=entities)
    assert f"e:{farm}" not in added_ids(OWNER, linked)


# --- bounds ------------------------------------------------------------------


def test_expand_input_bound(client, channel, monkeypatch):
    m = thread(client, 5)
    handle = make(OWNER, refs(m))
    monkeypatch.setenv("THROUGHLINE_EXPAND_MAX_INPUT", "4")
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, handle=handle, mode="context")
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, ids=m, mode="context")
    monkeypatch.setenv("THROUGHLINE_EXPAND_MAX_INPUT", "5")
    assert expand(OWNER, handle=handle, mode="context")
    assert expand_module.max_input() == 5
    monkeypatch.delenv("THROUGHLINE_EXPAND_MAX_INPUT")
    assert expand_module.max_input() == 2000
    r = client.post("/v1/expand", json=dict(ids=list(range(1, 2002)), mode="context"))
    assert r.status_code == 400, r.text
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, handle=handle, ids=m, mode="context")
    with pytest.raises(ExpandUnavailable):
        expand(OWNER, handle=handle, mode="nearby")


# --- ACL-8 -------------------------------------------------------------------


def test_acl_8_expand_never_reaches_hidden_rows(client, channel):
    register(client, "fake.hidden")
    put(
        client,
        [
            message(0, external_id="fake-0", thread={"external_id": "seen"}),
            message(1, external_id="fake-1", thread={"external_id": "seen"}),
            message(
                0,
                channel="fake.hidden",
                external_id="secret-0",
                thread={"external_id": "secret"},
                fields={},
                content="Synthetic secret at the same time",
            ),
        ],
    )
    found, hidden = ids(), ids("fake.hidden")
    secret = hidden["secret-0"]
    link("reply_to", "message", found["fake-0"], "message", secret)
    farm = entity("concept", "Synthetic Hidden Farm")
    link("about", "message", found["fake-0"], "entity", farm)
    guest = restricted("synthetic.chat")
    handle = make(guest, refs([found["fake-0"]]))
    for mode in ("context", "thread", "links", "time", "sources"):
        out = expand(guest, handle=handle, mode=mode, window_s=60)
        rows = owner(
            "SELECT record_id FROM tl.result_set_members WHERE handle=%s", (out,)
        )
        stored = {r["record_id"] for r in rows}
        assert f"m:{secret}" not in stored, mode
        assert f"e:{farm}" not in stored, mode
    # The same anchor seen by the owner reaches them.
    mine = make(OWNER, refs([found["fake-0"]]))
    assert f"m:{secret}" in added_ids(OWNER, expand(OWNER, handle=mine, mode="links"))
    assert f"m:{secret}" in added_ids(
        OWNER, expand(OWNER, handle=mine, mode="time", window_s=60)
    )
    # A hidden anchor named by id is not expanded at all.
    out = expand(guest, ids=[f"m:{secret}"], mode="time", window_s=60)
    assert resultsets.members(guest, out) == []


# --- completeness ------------------------------------------------------------


def test_completeness_per_mode_and_inherited(client, channel, monkeypatch):
    m = thread(client, 8)
    exhaustive = make(OWNER, refs([m[3]]))
    sample = make(OWNER, refs([m[3]]), completeness="ranked_sample")
    truncated = make(OWNER, refs([m[3]]), completeness="truncated")
    info = resultsets.info
    assert (
        info(OWNER, expand(OWNER, handle=exhaustive, mode="context"))["completeness"]
        == "exhaustive"
    )
    assert (
        info(OWNER, expand(OWNER, handle=sample, mode="context"))["completeness"]
        == "ranked_sample"
    )
    assert (
        info(OWNER, expand(OWNER, handle=truncated, mode="context"))["completeness"]
        == "truncated"
    )
    monkeypatch.setenv("THROUGHLINE_EXPAND_THREAD_CAP", "3")
    capped = expand(OWNER, handle=exhaustive, mode="thread")
    assert info(OWNER, capped)["completeness"] == "truncated"
    assert len(added_ids(OWNER, capped)) == 3
    assert (
        info(OWNER, expand(OWNER, handle=exhaustive, mode="focus", focus="orchard"))[
            "completeness"
        ]
        == "ranked_sample"
    )


# --- HTTP and CLI ------------------------------------------------------------


def token_scope():
    row = owner(
        "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id LIMIT 1"
    )[0]
    return Scope.from_token(dict(row, asker=None))


def test_expand_route_envelope_and_cli(client, channel):
    m = thread(client, 4)
    handle = make(token_scope(), refs([m[1]]))
    r = client.post("/v1/expand", json=dict(input=handle, mode="context", before=1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"records", "result_set", "execution"}
    assert body["execution"]["completeness"] in ("exhaustive", "truncated")
    assert [x["record_id"] for x in body["records"]][:1] == [f"m:{m[1]}"]
    assert {x["record_id"] for x in body["records"][1:]} == {
        f"m:{m[0]}",
        f"m:{m[2]}",
        f"m:{m[3]}",
    }
    # Another caller's handle is the same 404 as an unknown one.
    foreign = make(OWNER, refs([m[1]]))
    assert (
        client.post("/v1/expand", json=dict(input=foreign, mode="context")).status_code
        == 404
    )
    zero = client.post("/v1/expand", json=dict(input="rs_" + "0" * 32, mode="context"))
    assert zero.status_code == 404

    from throughline.cli_commands import semantic_ops

    commands = {}

    class Group:
        def command(self, name):
            def wrap(fn):
                commands[name] = fn
                return fn

            return wrap

    semantic_ops.register(Group())
    assert set(commands) == {"expand", "match"}


@pytest.mark.parametrize(
    "mode", ["thread", "sources", "links", "time", "place", "focus"]
)
def test_qry_32_every_mode_set_based(client, channel, monkeypatch, mode):
    m = thread(client, 41)
    for i in range(0, 40, 2):
        link("reply_to", "message", m[i], "message", m[i + 1])
    few = make(OWNER, refs(m[0:4:2]))
    many = make(OWNER, refs(m[0:40:2]))
    options = dict(mode=mode, window_s=60, focus="synthetic", before=1, after=1)
    counter = Statements(monkeypatch)
    expand(OWNER, handle=few, **options)
    small_count, counter.n = counter.n, 0
    expand(OWNER, handle=many, **options)
    assert counter.n <= small_count, (mode, counter.n, small_count)
