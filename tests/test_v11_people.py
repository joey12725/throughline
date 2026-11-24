"""Person-centric read model (migration 0047, throughline.people): the brief as the default entry point, the exchange index, circle and
ties, dyad versus author, the owner side by is_me, newest relevant last
contact, open loops, interactions, rhythm, arc, around, day, together,
mentions, dms, index staleness and purge. Synthetic data only."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import typer
from conftest import ingest
from psycopg import sql
from typer.testing import CliRunner

from throughline import access, people
from throughline.allowlist import load as load_allowlist
from throughline.db import connect, create_token

CH = "synthetic.people"
SECRET = "synthetic.people.secret"
FIELDS = [
    dict(name=n, type="text", description="Synthetic " + n)
    for n in (
        "direction",
        "conversation_kind",
        "identity_quality",
        "time_precision",
        "ignore_reason",
    )
]
T0 = datetime(2020, 3, 2, 10, 0, tzinfo=timezone.utc)  # a Monday
PEOPLE_FUNCTIONS = {
    "person_brief",
    "person_timeline",
    "person_arc",
    "dms_with",
    "between_threads",
    "between",
    "last_contact",
    "open_loops",
    "interactions",
    "around",
    "day",
    "together",
    "mentions",
    "circle",
    "ties",
    "rhythm",
    "attribution",
    "suspected_attributions",
    "exchange_index_status",
}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def headers(kind, askers=None, asker=None):
    token = create_token("synthetic-" + kind, kind, None, askers)["token"]
    out = {"Authorization": "Bearer " + token}
    if asker:
        out["X-Throughline-Asker"] = asker
    return out


def owner_rows(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


@contextmanager
def reader(asker=None):
    """A reader connection: unrestricted, or as a restricted asker."""
    with connect("reader") as c:
        if asker:
            for name, value in (("scope_mode", "asker"), ("asker", asker)):
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        yield c


def q(statement, params=(), asker=None):
    with reader(asker) as c:
        return c.execute(statement, params).fetchall()


class World:
    """Builds synthetic channels, people, accounts and messages."""

    def __init__(self, client):
        self.client = client
        self.n = 0
        self.ids = {}
        for name in (CH, SECRET):
            ok(
                client.post(
                    "/v1/channels",
                    json=dict(name=name, plugin="synthetic", fields=FIELDS),
                )
            )
        self.owner = self.person("synthetic-owner", "Synthetic Owner", owner=True)

    def person(self, handle, display=None, owner=False):
        row = ok(
            self.client.post(
                "/v1/identity/people",
                json=dict(handle=handle, display_name=display, is_owner=owner),
            )
        )
        return row["id"]

    def say(
        self,
        key,
        thread,
        account,
        at,
        content=None,
        channel=CH,
        **fields,
    ):
        """One message; at is seconds after T0 or a datetime."""
        self.n += 1
        when = at if isinstance(at, datetime) else T0 + timedelta(seconds=at)
        item = dict(
            channel=channel,
            external_id=f"syn-{key}",
            thread={"external_id": thread},
            happened_at=when.isoformat(),
            content=content or f"Synthetic orchard line {key}",
            author=dict(system="fake", kind="account", value=account),
            fields=fields,
            payload={"synthetic": key},
        )
        return item

    def load(self, *items):
        response = ingest(self.client, list(items))
        assert response.status_code == 200, response.text
        keys = [i["external_id"] for i in items]
        for row in owner_rows(
            "SELECT id,external_id FROM tl.messages WHERE external_id=ANY(%s) AND superseded_by IS NULL",
            (keys,),
        ):
            self.ids[row["external_id"][4:]] = row["id"]
        return self

    def identifier(self, account):
        rows = owner_rows("SELECT id FROM tl.identifiers WHERE value=%s", (account,))
        assert rows, account
        return rows[0]["id"]

    def attach(self, account, person_id):
        ok(
            self.client.post(
                "/v1/identity/attach",
                json=dict(identifier_id=self.identifier(account), person_id=person_id),
            )
        )

    def id(self, key):
        return self.ids[key]

    def index(self):
        return people.refresh(budget_ms=None)


def run(name, asker=None, **arguments):
    return q(people.statement(name, **arguments), asker=asker)


def owner_me(world, key, thread, at, content=None, **fields):
    """An owner message by direction only (the owner account is unattached)."""
    return world.say(
        key, thread, "me-acct", at, content, direction="outgoing", **fields
    )


def dyad(client):
    """Owner with alice (3 exchanges in one dm episode), bob and carol (one
    exchange each in their dms)."""
    w = World(client)
    w.alice = w.person("synthetic-alice", "Alice Synthetic")
    w.bob = w.person("synthetic-bob", "Bob Synthetic")
    w.carol = w.person("synthetic-carol", "Carol Synthetic")
    w.load(
        w.say("a1", "dm-alice", "alice-acct", 0, conversation_kind="dm"),
        owner_me(w, "o1", "dm-alice", 60, conversation_kind="dm"),
        w.say("a2", "dm-alice", "alice-acct", 120, conversation_kind="dm"),
        owner_me(w, "o2", "dm-alice", 180, conversation_kind="dm"),
        w.say("b1", "dm-bob", "bob-acct", 1000),
        owner_me(w, "o3", "dm-bob", 1060),
        w.say("c1", "dm-carol", "carol-acct", 2000),
        owner_me(w, "o4", "dm-carol", 2060),
    )
    w.attach("alice-acct", w.alice)
    w.attach("bob-acct", w.bob)
    w.attach("carol-acct", w.carol)
    w.index()
    return w


def restricted_asker(name="synthetic-asker", channel=CH):
    access.register_asker(name)
    access.grant(name, channel=channel)
    return name


# --- P3 brief ------------------------------------------------------------------------


def test_p3_brief_default_entry_sections(client):
    w = dyad(client)
    with connect() as c:
        c.execute(
            "INSERT INTO tl.person_names(person_id,name,retired_at) VALUES (%s,'Ali Synthetic',NULL)",
            (w.alice,),
        )
        c.execute(
            "INSERT INTO tl.fact_keys(key) VALUES ('synthetic_role') ON CONFLICT DO NOTHING"
        )
        c.execute(
            "INSERT INTO tl.facts(person_id,key,value) VALUES (%s,'synthetic_role','\"gardener\"')",
            (w.alice,),
        )
    body = ok(client.get(f"/v1/people/{w.alice}/brief"))
    brief = body["brief"]
    assert brief["person"] == w.alice
    assert brief["handle"] == "synthetic-alice"
    assert brief["display_name"] == "Alice Synthetic"
    assert brief["names"] == ["Ali Synthetic"]
    assert brief["identifiers"] == {"fake": {"confirmed": 1}}
    assert brief["message_count"] == 2
    assert brief["messages_to"] == 2
    assert brief["exchange_count"] == 3
    assert brief["top_channels"] == [{"channel": CH, "messages": 4}]
    assert brief["recent_threads"][0]["channel"] == CH
    assert brief["last_contact_message_id"] == w.id("o2")
    assert brief["first_contact_message_id"] == w.id("a1")
    assert brief["circle_rank"] == 1
    assert brief["facts"] == [
        {"key": "synthetic_role", "value": "gardener", "as_of": None}
    ]
    assert isinstance(brief["relations"], list)
    assert [c["channel"] for c in brief["coverage"]] == [CH]
    assert brief["open_loop_count"] == 0
    assert body["execution"]["status"] == "complete"
    # A restricted asker gets the counts over granted rows, no identity parts.
    asker = restricted_asker()
    agent = headers("agent", [asker])
    hidden = ok(client.get(f"/v1/people/{w.alice}/brief", headers=agent))["brief"]
    for key in ("handle", "display_name", "names", "identifiers", "facts", "relations"):
        assert hidden[key] is None, key
    assert hidden["message_count"] == 2 and hidden["exchange_count"] == 3
    # The catalogue names the brief as the first step.
    section = q("SELECT api.describe('people') d")[0]["d"]
    assert section["first_step"] == "person_brief"
    assert "self_direction" in section["attribution_statuses"]
    assert "exchange" in section and "units" in section


# --- exchange index --------------------------------------------------------------------


def test_p3_exchange_definition(client):
    w = World(client)
    w.bob = w.person("synthetic-bob")
    w.load(
        # Next turn exactly 30 minutes later counts.
        w.say("x1a", "x1", "alice-acct", 0),
        owner_me(w, "x1o", "x1", 1800),
        # 31 minutes does not.
        w.say("x2a", "x2", "alice-acct", 0),
        owner_me(w, "x2o", "x2", 1860),
        # The same author does not.
        w.say("x3a", "x3", "alice-acct", 0),
        w.say("x3b", "x3", "alice-acct", 60),
        # An ignored turn is skipped: the owner answers alice.
        w.say("x4a", "x4", "alice-acct", 0),
        w.say("x4b", "x4", "bob-acct", 60, ignore_reason="synthetic noise"),
        owner_me(w, "x4o", "x4", 120),
        # A reply counts across any gap.
        w.say("x5a", "x5", "alice-acct", 0),
        owner_me(w, "x5o", "x5", 5 * 3600),
    )
    with connect() as c:
        c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method) VALUES ('reply_to','message',%s,'message',%s,'source')",
            (w.id("x5o"), w.id("x5a")),
        )
    w.index()
    found = {
        (
            r["responder_external_id"][4:],
            r["target_external_id"][4:],
            r["method"],
            r["gap_s"],
        )
        for r in owner_rows("SELECT * FROM tl.exchanges")
    }
    assert found == {
        ("x1o", "x1a", "next_turn", 1800),
        ("x4o", "x4a", "next_turn", 120),
        ("x5o", "x5a", "reply", 18000),
    }
    # Index keys are external keys: a new version keeps the exchange.
    item = owner_me(w, "x1o", "x1", 1800, content="Synthetic edited reply")
    w.load(item)
    w.index()
    assert ("syn-x1o", "syn-x1a") in {
        (r["responder_external_id"], r["target_external_id"])
        for r in owner_rows("SELECT * FROM tl.exchanges")
    }


def test_p3_circle_absolute_and_relative(client):
    w = dyad(client)
    rows = run("circle", max_results=None)
    assert [
        (
            r["other_person_id"],
            r["exchanges"],
            r["shared_conversations"],
            r["strength"],
            r["rank"],
        )
        for r in rows
    ] == [
        (w.alice, 3, 1, 4, 1),
        (w.bob, 1, 1, 2, 2),
        (w.carol, 1, 1, 2, 3),
    ]
    assert [float(r["relative_strength"]) for r in rows] == [
        round(4 / 8, 6),
        round(2 / 8, 6),
        round(2 / 8, 6),
    ]
    relative = run("circle", mode="relative", max_results=2)
    assert [(r["other_person_id"], r["rank"]) for r in relative] == [
        (w.alice, 1),
        (w.bob, 2),
    ]
    # From alice's side the owner is her only tie.
    assert [
        (r["other_person_id"], r["strength"]) for r in run("circle", person_id=w.alice)
    ] == [(w.owner, 4)]
    # A window with nothing gives no rows and no division by zero.
    assert run("circle", after_at=T0 + timedelta(days=400)) == []
    body = ok(client.get(f"/v1/people/{w.owner}/circle", params={"mode": "relative"}))
    assert [r["other_person_id"] for r in body["rows"]] == [w.alice, w.bob, w.carol]


def test_p3_ties_pair_breakdown(client):
    w = dyad(client)
    (row,) = run("ties", a=w.owner, b=w.alice)
    assert row["person_a"] == w.owner and row["person_b"] == w.alice
    assert row["exchanges"] == 3
    assert row["a_responded"] == 2 and row["b_responded"] == 1
    assert row["shared_conversations"] == 1 and row["shared_threads"] == 1
    assert row["strength"] == 4
    assert row["first_exchange_at"] == T0 + timedelta(seconds=60)
    assert row["last_exchange_at"] == T0 + timedelta(seconds=180)
    (none,) = run("ties", a=w.bob, b=w.alice)
    assert none["exchanges"] == 0 and none["strength"] == 0
    body = ok(client.get("/v1/people/ties", params={"a": w.owner, "b": w.bob}))
    assert body["rows"][0]["strength"] == 2


# --- dyad versus author ------------------------------------------------------------


def dyad_world(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    w.carol = w.person("synthetic-carol")
    w.load(
        w.say("g1", "group", "alice-acct", 0),
        w.say("g2", "group", "bob-acct", 60),
        w.say("g3", "group", "carol-acct", 120),
        w.say("g4", "group", "alice-acct", 180),
        w.say("s1", "solo", "alice-acct", 1000),
        w.say("s2", "solo", "carol-acct", 1060),
    )
    for account, person in (
        ("alice-acct", w.alice),
        ("bob-acct", w.bob),
        ("carol-acct", w.carol),
    ):
        w.attach(account, person)
    w.index()
    return w


def m_ids(records):
    return {r["message_id"] for r in records if r["record_id"].startswith("m:")}


def test_dyad_vs_author(client):
    w = dyad_world(client)
    both = {w.id(k) for k in ("g1", "g2", "g4")}
    assert {r["message_id"] for r in run("between", persons=[w.alice, w.bob])} == both
    assert [
        r["thread_id"] for r in run("between_threads", persons=[w.alice, w.bob])
    ] == [
        owner_rows("SELECT thread_id FROM tl.messages WHERE id=%s", (w.id("g1"),))[0][
            "thread_id"
        ]
    ]
    # Author-only: alice's messages everywhere, never bob's.
    author = ok(
        client.post(
            "/v1/recall",
            json=dict(query="", scope=dict(persons=[w.alice]), exhaustive=True, k=50),
        )
    )
    assert m_ids(author["records"]) == {w.id(k) for k in ("g1", "g4", "s1")}
    # Recall's between role equals api.between.
    between = ok(
        client.post(
            "/v1/recall",
            json=dict(
                query="",
                scope=dict(persons=[w.alice, w.bob], person_role="between"),
                exhaustive=True,
                k=50,
            ),
        )
    )
    assert m_ids(between["records"]) == both
    # Keyword recall under the same role stays inside the dyad.
    keyword = ok(
        client.post(
            "/v1/recall",
            json=dict(
                query="orchard",
                scope=dict(persons=[w.alice, w.bob], person_role="between"),
                exhaustive=True,
                k=50,
            ),
        )
    )
    assert m_ids(keyword["records"]) <= both and m_ids(keyword["records"])
    # Between needs two persons; no shared thread gives nothing, with a warning.
    one = client.post(
        "/v1/recall",
        json=dict(query="", scope=dict(persons=[w.alice], person_role="between")),
    )
    assert one.status_code == 400, one.text
    loner = w.person("synthetic-loner")
    empty = ok(
        client.post(
            "/v1/recall",
            json=dict(
                query="",
                scope=dict(persons=[w.alice, loner], person_role="between"),
                exhaustive=True,
            ),
        )
    )
    assert m_ids(empty["records"]) == set()
    assert "no_shared_threads" in empty["execution"]["warnings"]
    http = ok(client.get("/v1/between", params={"persons": f"{w.alice},{w.bob}"}))
    assert {r["message_id"] for r in http["rows"]} == both
    assert (
        client.get("/v1/between", params={"persons": str(w.alice)}).status_code == 400
    )


def test_owner_side_by_direction_without_person_id(client):
    w = World(client)
    w.bob = w.person("synthetic-bob")
    w.load(
        w.say("b1", "dm-bob", "bob-acct", 0),
        owner_me(w, "o1", "dm-bob", 60),
        w.say("b2", "dm-bob", "bob-acct", 120),
    )
    w.attach("bob-acct", w.bob)
    w.index()
    # The owner message has no person_id, yet it is on the owner's side.
    (appearance,) = q(
        "SELECT * FROM api.appearances WHERE message_id=%s", (w.id("o1"),)
    )
    assert appearance["person_id"] is None and appearance["is_me"] is True
    assert appearance["party_id"] == w.owner
    assert appearance["attribution_status"] == "self_direction"
    assert appearance["direction"] == "outgoing"
    assert {r["message_id"] for r in run("between", persons=[w.owner, w.bob])} == {
        w.id("b1"),
        w.id("o1"),
        w.id("b2"),
    }
    assert [(r["other_person_id"], r["exchanges"]) for r in run("circle")] == [
        (w.bob, 2)
    ]
    (tie,) = run("ties", a=w.owner, b=w.bob)
    assert tie["exchanges"] == 2 and tie["a_responded"] == 1
    sides = {r["message_id"]: r["side"] for r in run("dms_with", person_id=w.bob)}
    assert sides == {w.id("b1"): "from", w.id("o1"): "to", w.id("b2"): "from"}


def test_newest_relevant_last_contact(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.carol = w.person("synthetic-carol")
    w.load(
        w.say("g1", "group", "alice-acct", 0),
        owner_me(w, "g2", "group", 60),
        # Hours later someone else writes in the shared thread: the newest
        # absolute record, but not contact with alice.
        w.say("g3", "group", "carol-acct", 9000),
    )
    w.attach("alice-acct", w.alice)
    w.attach("carol-acct", w.carol)
    w.index()
    (row,) = run("last_contact", person_id=w.alice)
    assert row["last_from_message_id"] == w.id("g1")
    assert row["last_to_message_id"] == w.id("g2")
    assert row["last_exchange_message_id"] == w.id("g2")
    assert row["last_contact_message_id"] == w.id("g2")
    assert row["last_contact_at"] == T0 + timedelta(seconds=60)
    body = ok(client.get(f"/v1/people/{w.alice}/last-contact"))
    assert body["last_contact"]["last_contact_message_id"] == w.id("g2")


# --- open loops ------------------------------------------------------------------------


def test_open_loops_either_direction_after_a_day_wound_down_excluded(client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ago = lambda **k: now - timedelta(**k)  # noqa: E731
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    w.carol = w.person("synthetic-carol")
    w.load(
        # The owner asked alice three days ago: they owe.
        owner_me(
            w, "a1", "dm-alice", ago(days=3), "Synthetic, can you send the orchard map?"
        ),
        w.say("a0", "dm-alice", "alice-acct", ago(days=4), "Synthetic hello"),
        # bob asked the owner two days ago: the owner owes.
        w.say(
            "b1", "dm-bob", "bob-acct", ago(days=2), "Synthetic are you free tomorrow"
        ),
        owner_me(w, "b0", "dm-bob", ago(days=5), "Synthetic hi bob"),
        # carol's question was answered.
        w.say("c1", "dm-carol", "carol-acct", ago(days=2), "Synthetic orchard open?"),
        owner_me(w, "c2", "dm-carol", ago(days=1), "Synthetic yes it is"),
        # The owner closed their own question.
        w.say("d0", "dm-dave", "dave-acct", ago(days=5), "Synthetic hey"),
        owner_me(
            w, "d1", "dm-dave", ago(days=3), "Synthetic could you check the gate?"
        ),
        owner_me(w, "d2", "dm-dave", ago(days=2), "Synthetic never mind, sorted"),
        # A chat with no activity for 30 days is wound down.
        w.say("e1", "dm-erin", "erin-acct", ago(days=40), "Synthetic any news?"),
        owner_me(w, "e0", "dm-erin", ago(days=41), "Synthetic hello erin"),
        # Too recent to be a loop.
        w.say("f1", "dm-frank", "frank-acct", ago(hours=2), "Synthetic lunch?"),
        owner_me(w, "f0", "dm-frank", ago(hours=3), "Synthetic hi frank"),
    )
    for account, person in (
        ("alice-acct", w.alice),
        ("bob-acct", w.bob),
        ("carol-acct", w.carol),
    ):
        w.attach(account, person)
    w.index()
    rows = run("open_loops")
    assert {(r["message_id"], r["direction"]) for r in rows} == {
        (w.id("a1"), "they_owe"),
        (w.id("b1"), "owner_owes"),
    }
    by = {r["message_id"]: r for r in rows}
    assert by[w.id("b1")]["asker_party_id"] == w.bob
    assert by[w.id("a1")]["asker_party_id"] == w.owner
    assert float(by[w.id("a1")]["age_days"]) >= 3
    assert [
        (r["message_id"], r["direction"]) for r in run("open_loops", person_id=w.alice)
    ] == [(w.id("a1"), "they_owe")]
    assert [r["message_id"] for r in run("open_loops", person_id=w.bob)] == [w.id("b1")]
    # Shorter age threshold: frank's question is now open too.
    assert w.id("f1") in {
        r["message_id"] for r in run("open_loops", older_than="1 hour")
    }
    # A longer horizon brings erin's old chat back.
    assert w.id("e1") in {r["message_id"] for r in run("open_loops", horizon_days=60)}
    body = ok(client.get("/v1/open-loops"))
    assert {r["message_id"] for r in body["rows"]} == {w.id("a1"), w.id("b1")}
    body = ok(client.get(f"/v1/people/{w.alice}/open-loops"))
    assert [r["message_id"] for r in body["rows"]] == [w.id("a1")]


def test_open_loops_one_to_one_threads_the_owner_never_answered(client):
    """Migration 0053: an ask alone in a one-to-one thread is still a loop;
    group threads and threads with unattributed authors need an owner-side
    message; in a one-to-one channel an owner ask alone in its thread has
    the channel's person as counterpart."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ago = lambda **k: now - timedelta(**k)  # noqa: E731
    w = World(client)
    one = "synthetic.people.one"
    ok(
        client.post(
            "/v1/channels", json=dict(name=one, plugin="synthetic", fields=FIELDS)
        )
    )
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    w.carol = w.person("synthetic-carol")
    w.dana = w.person("synthetic-dana")
    w.load(
        # alice asked in a thread of her own; the owner never wrote there.
        w.say("s1", "solo-alice", "alice-acct", ago(days=2), "Synthetic can you call?"),
        # A group thread without the owner is not the owner's loop.
        w.say("g1", "grp", "bob-acct", ago(days=2), "Synthetic who has the key?"),
        w.say("g0", "grp", "carol-acct", ago(days=3), "Synthetic hello all"),
        # An unattributed author makes the thread not one-to-one.
        w.say("m1", "mixed", "alice-acct", ago(days=2), "Synthetic any news?"),
        w.say("m0", "mixed", "stranger-acct", ago(days=3), "Synthetic hi"),
        # A one-to-one channel with dana: the owner's ask sits alone.
        w.say(
            "d0", "d-hello", "dana-acct", ago(days=4), "Synthetic hello", channel=one
        ),
        owner_me(
            w, "d1", "d-ask", ago(days=2), "Synthetic can you send it?", channel=one
        ),
    )
    for account, person in (
        ("alice-acct", w.alice),
        ("bob-acct", w.bob),
        ("carol-acct", w.carol),
        ("dana-acct", w.dana),
    ):
        w.attach(account, person)
    w.index()
    every = {(r["message_id"], r["direction"]) for r in run("open_loops")}
    assert every == {(w.id("s1"), "owner_owes"), (w.id("d1"), "they_owe")}
    alice = run("open_loops", person_id=w.alice)
    assert [(r["message_id"], r["counterpart_party_id"]) for r in alice] == [
        (w.id("s1"), w.owner)
    ]
    dana = run("open_loops", person_id=w.dana)
    assert [
        (r["message_id"], r["direction"], r["counterpart_party_id"]) for r in dana
    ] == [(w.id("d1"), "they_owe", w.dana)]
    assert run("open_loops", person_id=w.bob) == []
    # The asker gate decides: an asker granted only the shared channel sees
    # alice's loop and nothing of the one-to-one channel.
    guest = restricted_asker()
    assert [r["message_id"] for r in run("open_loops", asker=guest)] == [w.id("s1")]
    assert run("open_loops", asker=guest, person_id=w.dana) == []
    body = ok(client.get(f"/v1/people/{w.alice}/open-loops"))
    assert [r["message_id"] for r in body["rows"]] == [w.id("s1")]


# --- interactions, rhythm, arc ------------------------------------------------------


def test_p3_interactions_window_and_rhythm_counts(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    day = 86400
    w.load(
        w.say("a1", "dm", "alice-acct", 0, conversation_kind="dm"),  # Mon 10:00
        owner_me(w, "o1", "dm", 60, conversation_kind="dm"),
        w.say(
            "a2", "dm", "alice-acct", day + 3600, conversation_kind="dm"
        ),  # Tue 11:00
        w.say(
            "a3", "dm", "alice-acct", 2 * day + 3600, conversation_kind="dm"
        ),  # Wed 11:00
        w.say("a4", "dm", "alice-acct", 9 * day, conversation_kind="dm"),  # next Wed
    )
    w.attach("alice-acct", w.alice)
    w.index()
    rows = run(
        "interactions",
        person_id=w.alice,
        after_at=T0,
        before_at=T0 + timedelta(days=3),
        grain="day",
    )
    assert [
        (r["period"], r["messages_from"], r["messages_to"], r["exchanges"])
        for r in rows
    ] == [
        ("2020-03-02", 1, 1, 1),
        ("2020-03-03", 1, 0, 0),
        ("2020-03-04", 1, 0, 0),
        # The window ends at 10:00 on the 5th: a partial, empty period.
        ("2020-03-05", 0, 0, 0),
    ]
    assert rows[0]["channels"] == [CH] and rows[3]["channels"] == []
    weekly = run("interactions", person_id=w.alice, grain="week")
    assert [(r["period"], r["messages_from"]) for r in weekly] == [
        ("2020-W10", 3),
        ("2020-W11", 1),
    ]
    rhythm = run("rhythm", person_id=w.alice, tz="UTC")
    cells = {
        (r["weekday"], r["hour"]): r["messages"]
        for r in rhythm
        if r["dimension"] == "weekday_hour"
    }
    assert cells == {(1, 10): 1, (2, 11): 1, (3, 11): 1, (3, 10): 1}
    assert [
        (r["month"], r["messages"]) for r in rhythm if r["dimension"] == "month"
    ] == [(3, 4)]
    # POSIX offset (UTC-9 is nine hours ahead): the test server has no zone
    # database.
    shifted = run("rhythm", person_id=w.alice, tz="UTC-9")
    assert (1, 19) in {
        (r["weekday"], r["hour"]) for r in shifted if r["dimension"] == "weekday_hour"
    }
    body = ok(
        client.get(
            f"/v1/people/{w.alice}/interactions",
            params={
                "grain": "day",
                "after": T0.isoformat(),
                "before": (T0 + timedelta(days=1)).isoformat(),
            },
        )
    )
    assert [r["messages_from"] for r in body["rows"]] == [1, 0]
    assert ok(client.get(f"/v1/people/{w.alice}/rhythm"))["row_count"] == 5


def test_p3_arc_first_peak_last_periods(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    jan = datetime(2020, 1, 15, 12, tzinfo=timezone.utc)
    mar = datetime(2020, 3, 10, 12, tzinfo=timezone.utc)
    apr = datetime(2020, 4, 20, 12, tzinfo=timezone.utc)
    w.load(
        w.say("j1", "dm", "alice-acct", jan, conversation_kind="dm"),
        w.say("m1", "dm", "alice-acct", mar, conversation_kind="dm"),
        owner_me(w, "m2", "dm", mar + timedelta(minutes=1), conversation_kind="dm"),
        w.say(
            "m3", "dm", "alice-acct", mar + timedelta(minutes=2), conversation_kind="dm"
        ),
        w.say("p1", "dm", "alice-acct", apr, conversation_kind="dm"),
    )
    w.attach("alice-acct", w.alice)
    w.index()
    rows = run("person_arc", person_id=w.alice, grain="month")
    assert [
        (r["period"], r["messages_from"], r["messages_to"], r["exchanges"])
        for r in rows
    ] == [
        ("2020-01", 1, 0, 0),
        ("2020-02", 0, 0, 0),
        ("2020-03", 2, 1, 2),
        ("2020-04", 1, 0, 0),
    ]
    first = rows[0]
    assert (first["first_period"], first["peak_period"], first["last_period"]) == (
        "2020-01",
        "2020-03",
        "2020-04",
    )
    assert first["last_contact_at"] == apr and float(first["gap_days"]) > 365
    quarters = run("person_arc", person_id=w.alice, grain="quarter")
    assert [r["period"] for r in quarters] == ["2020-Q1", "2020-Q2"]
    assert quarters[0]["peak_period"] == "2020-Q1"
    assert (
        client.get(f"/v1/people/{w.alice}/arc", params={"grain": "decade"}).status_code
        == 422
    )
    body = ok(client.get(f"/v1/people/{w.alice}/arc", params={"grain": "year"}))
    assert [(r["period"], r["messages_from"]) for r in body["rows"]] == [("2020", 4)]
    # A person without contact has no arc (and no division by zero).
    assert run("person_arc", person_id=w.owner + 999) == []


# --- around, day, together, mentions, dms ------------------------------------------


def test_p3_around_and_day_use_effective_time_and_exclude_ignored(client):
    w = World(client)
    june = datetime(2020, 6, 1, 12, tzinfo=timezone.utc)
    w.load(
        # Recorded in May; its effective time (annotation) is June 1 12:00.
        w.say("m1", "t1", "alice-acct", datetime(2020, 5, 1, 12, tzinfo=timezone.utc)),
        w.say("m2", "t2", "bob-acct", june + timedelta(minutes=10)),
        w.say(
            "m3",
            "t3",
            "carol-acct",
            june + timedelta(minutes=5),
            ignore_reason="synthetic noise",
        ),
        w.say("m4", "t4", "dave-acct", june + timedelta(hours=3)),
    )
    with connect() as c:
        c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) SELECT id,'syn-m1','effective_time',%s,'owner','owner' FROM tl.channels WHERE name=%s",
            (json.dumps({"at": june.isoformat(), "precision": "exact"}), CH),
        )
    rows = run("around", at=june, window_s=1800)
    assert [(r["message_id"], r["offset_s"]) for r in rows] == [
        (w.id("m1"), 0),
        (w.id("m2"), 600),
    ]
    assert rows[0]["record_time"] == datetime(2020, 5, 1, 12, tzinfo=timezone.utc)
    with_ignored = run("around", at=june, window_s=1800, include_ignored=True)
    assert [r["message_id"] for r in with_ignored] == [
        w.id("m1"),
        w.id("m3"),
        w.id("m2"),
    ]
    assert (
        run("around", at=datetime(2020, 5, 1, 12, tzinfo=timezone.utc), window_s=60)
        == []
    )
    day = run("day", on_date="2020-06-01", tz="UTC")
    assert [r["message_id"] for r in day] == [w.id("m1"), w.id("m2"), w.id("m4")]
    # In Tokyo 15:00 UTC is already June 2.
    tokyo = run("day", on_date="2020-06-02", tz="UTC-9")
    assert [r["message_id"] for r in tokyo] == [w.id("m4")]
    body = ok(
        client.get("/v1/around", params={"at": june.isoformat(), "window_s": 1800})
    )
    assert [r["message_id"] for r in body["rows"]] == [w.id("m1"), w.id("m2")]
    body = ok(
        client.get("/v1/day", params={"date": "2020-06-01", "include_ignored": "true"})
    )
    assert len(body["rows"]) == 4


def test_p3_together_and_mentions(client):
    w = World(client)
    w.alice = w.person("synthetic-alice", "Alice Synthetic")
    w.bob = w.person("synthetic-bob")
    w.carol = w.person("synthetic-carol")
    with connect() as c:
        c.execute(
            "INSERT INTO tl.person_names(person_id,name) VALUES (%s,'Alice')",
            (w.alice,),
        )
    hour = 3600
    w.load(
        # One thread, two episodes (gap over 30 minutes): alice and bob share
        # only the first.
        w.say("e1", "group", "alice-acct", 0),
        w.say("e2", "group", "bob-acct", 60, "Synthetic thanks Alice for the orchard"),
        w.say(
            "e3",
            "group",
            "alice-acct",
            2 * hour,
            "Synthetic I am Alice and say alice again",
        ),
        w.say(
            "e4",
            "group",
            "carol-acct",
            2 * hour + 60,
            "Synthetic alice's ladder is here",
        ),
        w.say("e5", "other", "carol-acct", 5 * hour, "Synthetic malice is not a name"),
    )
    for account, person in (
        ("alice-acct", w.alice),
        ("bob-acct", w.bob),
        ("carol-acct", w.carol),
    ):
        w.attach(account, person)
    w.index()
    (episode,) = run("together", persons=[w.alice, w.bob])
    assert episode["episode_id"] == f"ep:{episode['thread_id']}:{w.id('e1')}"
    assert episode["messages"] == 2 and episode["participants"] == sorted(
        [w.alice, w.bob]
    )
    grain = q(
        "SELECT episode_id FROM api.episodes_of(%s::bigint[])",
        ([w.id("e1"), w.id("e3")],),
    )
    assert episode["episode_id"] == grain[0]["episode_id"]
    assert len({g["episode_id"] for g in grain}) == 2
    assert (
        run("together", persons=[w.alice, w.bob], after_at=T0 + timedelta(hours=1))
        == []
    )
    mentions = run("mentions", person_id=w.alice)
    assert [r["message_id"] for r in mentions] == [w.id("e4"), w.id("e2")]
    assert {r["matched_term"] for r in mentions} == {"Alice"}
    # Alice's own message and 'malice' are not mentions.
    assert w.id("e3") not in {r["message_id"] for r in mentions}
    assert w.id("e5") not in {r["message_id"] for r in mentions}
    asker = restricted_asker()
    hidden = run("mentions", asker=asker, person_id=w.alice)
    assert [r["message_id"] for r in hidden] == [w.id("e4"), w.id("e2")]
    assert all(r["matched_term"] is None for r in hidden)
    body = ok(client.get("/v1/together", params={"persons": f"{w.alice},{w.bob}"}))
    assert body["row_count"] == 1


def test_p3_dms_with_both_sides(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    w.load(
        # Labelled one-to-one.
        w.say("d1", "dm-alice", "alice-acct", 0, conversation_kind="dm"),
        owner_me(w, "d2", "dm-alice", 60, conversation_kind="dm"),
        # Unlabelled, but its parties are exactly owner and alice.
        w.say("u1", "pair", "alice-acct", 1000),
        owner_me(w, "u2", "pair", 1060),
        # A group thread is not a dm.
        w.say("g1", "group", "alice-acct", 2000),
        w.say("g2", "group", "bob-acct", 2060),
        owner_me(w, "g3", "group", 2120),
    )
    w.attach("alice-acct", w.alice)
    w.attach("bob-acct", w.bob)
    w.index()
    rows = run("dms_with", person_id=w.alice)
    assert {(r["message_id"], r["side"]) for r in rows} == {
        (w.id("d1"), "from"),
        (w.id("d2"), "to"),
        (w.id("u1"), "from"),
        (w.id("u2"), "to"),
    }
    assert [r["message_id"] for r in rows][0] == w.id("u2")  # newest first
    body = ok(client.get(f"/v1/people/{w.alice}/dms", params={"max_results": 2}))
    assert body["row_count"] == 2


# --- index, purge, registry, help ------------------------------------------------------


def test_exchange_index_refresh_and_staleness_disclosed(client, monkeypatch):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.load(
        w.say("a1", "dm", "alice-acct", 0),
        owner_me(w, "o1", "dm", 60),
    )
    w.attach("alice-acct", w.alice)
    (state,) = run("exchange_index_status")
    assert state["stale_threads"] == 1
    monkeypatch.setenv("THROUGHLINE_PEOPLE_REFRESH_MS", "0")
    body = ok(client.get(f"/v1/people/{w.alice}/timeline"))
    execution = body["execution"]
    assert {"stage": "exchange_index", "reason": "exchange_index_stale"} in execution[
        "incomplete"
    ]
    assert "exchange_index_stale:1" in execution["warnings"]
    assert execution["exchange_index"]["stale_threads"] == 1
    monkeypatch.setenv("THROUGHLINE_PEOPLE_REFRESH_MS", "2000")
    body = ok(client.get(f"/v1/people/{w.alice}/timeline"))
    assert body["execution"]["incomplete"] == []
    assert body["execution"]["exchange_index"]["stale_threads"] == 0
    assert {r["side"] for r in body["rows"]} == {"from", "to"}
    (state,) = run("exchange_index_status")
    assert state["stale_threads"] == 0 and state["indexed_threads"] == 1
    # New messages, reply links and ignore annotations mark the thread stale.
    w.load(w.say("a2", "dm", "alice-acct", 5000))
    assert run("exchange_index_status")[0]["stale_threads"] == 1
    w.index()
    with connect() as c:
        c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method) VALUES ('reply_to','message',%s,'message',%s,'source')",
            (w.id("a2"), w.id("o1")),
        )
    assert run("exchange_index_status")[0]["stale_threads"] == 1
    w.index()
    assert ("syn-a2", "syn-o1", "reply") in {
        (r["responder_external_id"], r["target_external_id"], r["method"])
        for r in owner_rows("SELECT * FROM tl.exchanges")
    }
    with connect() as c:
        c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) SELECT id,'syn-o1','ignore','{\"ignored\": true}','owner','owner' FROM tl.channels WHERE name=%s",
            (CH,),
        )
    assert run("exchange_index_status")[0]["stale_threads"] == 1
    # A changed window re-indexes everything.
    w.index()
    monkeypatch.setenv("THROUGHLINE_EXCHANGE_WINDOW_S", "10")
    result = people.refresh(budget_ms=None)
    assert result["rebuilt"] == 1 and result["remaining"] == 0
    # The CLI index command reads the same state.
    body = ok(client.get("/v1/people/index"))
    assert body["rows"][0]["stale_threads"] == 0


def test_purge_removes_exchanges(client):
    from throughline.cli import purge

    w = dyad(client)
    assert owner_rows("SELECT count(*) AS n FROM tl.exchanges")[0]["n"] == 5
    purge(CH, confirm=True)
    assert owner_rows("SELECT count(*) AS n FROM tl.exchanges")[0]["n"] == 0
    assert owner_rows("SELECT count(*) AS n FROM tl.exchange_state")[0]["n"] == 0
    assert run("circle") == []
    assert owner_rows("SELECT 1 FROM tl.purge_hooks WHERE name='exchanges'")
    assert w.owner


def test_registry_examples_execute(client):
    World(client)
    registry = {
        r["name"]: r
        for r in owner_rows(
            "SELECT * FROM tl.api_functions WHERE name=ANY(%s)",
            (list(PEOPLE_FUNCTIONS),),
        )
    }
    assert set(registry) == PEOPLE_FUNCTIONS
    data = json.loads(
        (people.__file__.replace("people.py", "allowlist/0047_people.json"))
        and open(
            people.__file__.replace("people.py", "allowlist/0047_people.json"),
            encoding="utf-8",
        ).read()
    )
    assert set(data["functions"]) == PEOPLE_FUNCTIONS and data["scalar"] == []
    functions, _scalar = load_allowlist()
    assert PEOPLE_FUNCTIONS <= functions
    for name, row in registry.items():
        assert row["kind"] == "sql" and row["exactness"] == "exact"
        assert row["signature"].startswith(name + "("), row
        # Examples run for the reader on an empty database, unrestricted and
        # for a restricted asker.
        q(row["example"])
    asker = restricted_asker()
    for row in registry.values():
        q(row["example"], asker=asker)
    # Ranked-limited functions name their limit argument.
    assert registry["circle"]["ranked_limit_arg"] == 5
    assert registry["person_timeline"]["ranked_limit_arg"] == 4
    # Every function is reachable through validated SQL.
    body = ok(
        client.post("/v1/sql", json={"statement": "SELECT * FROM api.person_brief(1)"})
    )
    assert body["row_count"] == 1


def test_int_1_help_budget():
    from throughline import cli

    runner = CliRunner()
    text = ""
    for group in ("", "token", "identity", "track"):
        result = runner.invoke(
            cli.app, ([group] if group else []) + ["--help"], color=False
        )
        assert result.exit_code == 0, result.output
        text += result.output
    assert len(text) / 4 < 1500, len(text) / 4
    root = {c.name for c in cli.app.registered_commands}
    assert "person" not in root
    lib = runner.invoke(cli.app, ["lib", "--help"], color=False).output
    assert "person" in lib
    person = runner.invoke(cli.app, ["lib", "person", "--help"], color=False).output
    for name in (
        "brief",
        "timeline",
        "arc",
        "dms",
        "last",
        "loops",
        "circle",
        "ties",
        "rhythm",
        "mentions",
        "around",
        "day",
        "together",
        "between",
        "index",
        "status",
    ):
        assert name in person, name
    assert isinstance(cli.lib, typer.Typer)


def test_cli_person_commands_call_the_routes(monkeypatch):
    from throughline import cli

    calls = []

    def fake_request(method, path, data=None, params=None):
        calls.append((method, path, data, params))
        return {"rows": [{"ok": 1}], "row_count": 1, "execution": {"incomplete": []}}

    monkeypatch.setattr(cli, "request", fake_request)
    runner = CliRunner()
    for args in (
        ["brief", "7"],
        ["between", "1,2"],
        ["loops"],
        ["status", "5", "confirmed", "--reason", "synthetic"],
    ):
        result = runner.invoke(cli.app, ["lib", "person", *args, "--json"], color=False)
        assert result.exit_code == 0, result.output
    assert calls[0][:2] == ("GET", "people/7/brief")
    assert calls[1][1] == "between" and calls[1][3]["persons"] == "1,2"
    assert calls[2][1] == "open-loops"
    assert calls[3][:3] == (
        "POST",
        "identity/identifiers/5/status",
        {"status": "confirmed", "reason": "synthetic"},
    )


def test_between_falls_back_to_author_over_the_source_cap(client, monkeypatch):
    w = dyad_world(client)
    monkeypatch.setenv("THROUGHLINE_SCOPE_SOURCE_CAP", "1")
    body = ok(
        client.post(
            "/v1/recall",
            json=dict(
                query="",
                scope=dict(persons=[w.alice, w.bob], person_role="between"),
                exhaustive=True,
                k=50,
            ),
        )
    )
    assert "between_author_fallback" in body["execution"]["warnings"]
    # Narrowed to the listed authors in the shared threads, never widened.
    assert m_ids(body["records"]) == {w.id(k) for k in ("g1", "g2", "g4")}


def test_mentions_of_a_stop_word_name(client):
    w = World(client)
    w.will = w.person("synthetic-will")
    w.bob = w.person("synthetic-bob")
    with connect() as c:
        c.execute(
            "INSERT INTO tl.person_names(person_id,name) VALUES (%s,'Will')",
            (w.will,),
        )
    w.load(
        w.say("m1", "t", "bob-acct", 0, "Synthetic ask Will about the orchard"),
        w.say("m2", "t", "bob-acct", 60, "Synthetic willow by the orchard"),
        w.say("m3", "t", "will-acct", 120, "Synthetic Will here"),
    )
    w.attach("will-acct", w.will)
    w.attach("bob-acct", w.bob)
    rows = run("mentions", person_id=w.will)
    assert [r["message_id"] for r in rows] == [w.id("m1")]
    assert rows[0]["matched_term"] == "Will"
