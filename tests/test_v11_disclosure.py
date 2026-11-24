"""Sensitivity labels and disclosure policy in the database gate (migration
0051; ACL-1, ACL-2, ACL-3, ACL-5, ACL-7, ACL-8, ACL-10, FLT-3).

One world: a restricted asker 'guest' granted a public channel, a second
channel labelled 'work' and a page channel, without any label. The public
channel holds an open thread, a thread labelled 'intimate' and a message
whose access_label is 'medical'; the page cites a public message and the
medical one. Canaries mark every labelled row. Synthetic data only.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message, truncate_tl
from psycopg import sql
from typer.testing import CliRunner

from throughline import access
from throughline.db import connect, create_token

CANARY = "zqlabelcanary"
PUBLIC = "synthetic.disclosure"
WORK = "synthetic.disclosure.work"
PAGE = "synthetic.disclosure.page"
GUEST = "guest"
SEEDED = {"intimate", "medical", "financial", "work", "third_party_confidence"}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def flattened(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from flattened(inner.routes)
        else:
            yield route


def has_route(app, method, path):
    return any(
        getattr(r, "path", None) == path
        and method in (getattr(r, "methods", None) or ())
        for r in flattened(app.routes)
    )


@contextmanager
def reader(mode=None, asker=None, unknown=None):
    """A reader transaction with the settings the service would place."""
    with connect("reader") as c:
        for name, value in (
            ("scope_mode", mode),
            ("asker", asker),
            ("unknown_asker", unknown),
        ):
            if value is not None:
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        yield c


def as_asker(name=GUEST):
    return reader("asker", name, "deny")


def ids(statement, asker=GUEST, params=None):
    with as_asker(asker) as c:
        return sorted(r["id"] for r in c.execute(statement, params).fetchall())


def owner_rows(statement, params=None):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def leaks(value):
    return CANARY in json.dumps(value, default=str).lower()


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


def message_id(channel, external_id):
    return owner_rows(
        "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
        (channel, external_id),
    )[0]["id"]


@pytest.fixture()
def world(client):
    for name, extra in (
        (PUBLIC, {}),
        (WORK, {}),
        (PAGE, dict(is_reference=True, record_kind="page_revision")),
    ):
        ok(
            client.post(
                "/v1/channels", json=dict(name=name, plugin="synthetic", **extra)
            )
        )
    items = (
        [
            message(
                i * 60,
                channel=PUBLIC,
                external_id=f"pub-{i}",
                thread={"external_id": "open"},
                content=f"Synthetic orchard visit {i}",
                fields={},
            )
            for i in range(4)
        ]
        + [
            message(
                i * 60 + 10,
                channel=PUBLIC,
                external_id=f"prv-{i}",
                thread={"external_id": "private"},
                content=f"Synthetic orchard {CANARY} private {i}",
                fields={},
            )
            for i in range(3)
        ]
        + [
            message(
                500,
                channel=PUBLIC,
                external_id="med-0",
                thread={"external_id": "open"},
                content=f"Synthetic orchard {CANARY} medical note",
                fields={},
                access_label="medical",
            ),
            message(
                520,
                channel=PUBLIC,
                external_id="custom-0",
                thread={"external_id": "open"},
                content="Synthetic orchard custom tagged",
                fields={},
                access_label="synthetic_custom",
            ),
        ]
        + [
            message(
                i * 60 + 20,
                channel=WORK,
                external_id=f"wrk-{i}",
                thread={"external_id": "w"},
                content=f"Synthetic orchard {CANARY} work {i}",
                fields={},
            )
            for i in range(2)
        ]
        + [
            message(
                900,
                channel=PAGE,
                external_id="wiki",
                thread={"external_id": "wiki"},
                content="# Orchard\n\nPage about the orchard and its upkeep.",
                fields={},
                author=None,
            )
        ]
    )
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    ok(
        client.post(
            "/v1/provenance",
            json={
                "derived": {"channel": PAGE, "external_id": "wiki"},
                "sources": [
                    {"channel": PUBLIC, "external_id": "pub-0"},
                    {"channel": PUBLIC, "external_id": "med-0"},
                ],
            },
        )
    )
    m = {f"pub-{i}": message_id(PUBLIC, f"pub-{i}") for i in range(4)}
    m.update({f"prv-{i}": message_id(PUBLIC, f"prv-{i}") for i in range(3)})
    m.update({f"wrk-{i}": message_id(WORK, f"wrk-{i}") for i in range(2)})
    m["med"] = message_id(PUBLIC, "med-0")
    m["custom"] = message_id(PUBLIC, "custom-0")
    m["page"] = message_id(PAGE, "wiki")
    threads = {
        r["external_id"]: r["id"]
        for r in owner_rows("SELECT id,external_id FROM tl.threads")
    }
    access.label("intimate", thread_id=threads["private"], purpose="synthetic setup")
    access.label("work", channel=WORK, purpose="synthetic setup")
    access.register_asker(GUEST)
    for name in (PUBLIC, WORK, PAGE):
        access.grant(GUEST, channel=name, purpose="synthetic household agent")
    agent = create_token("guest agent", kind="agent", askers=[GUEST])
    as_agent = {"Authorization": "Bearer " + agent["token"]}
    return dict(client=client, m=m, threads=threads, agent=as_agent)


def public_ids(w, *keys):
    return sorted(w["m"][k] for k in keys)


OPEN = ("pub-0", "pub-1", "pub-2", "pub-3", "custom")
VISIBLE_MESSAGES = "SELECT id FROM api.messages"


# --- default deny and grants with labels -----------------------------------------


def test_p9_labelled_channel_hidden_even_when_granted(world):
    m = world["m"]
    # The page cites the medical message, so it is hidden too.
    assert ids(VISIBLE_MESSAGES) == public_ids(world, *OPEN)
    with as_asker() as c:
        assert m["wrk-0"] not in {r["id"] for r in c.execute(VISIBLE_MESSAGES)}
        channels = {r["name"] for r in c.execute("SELECT name FROM api.channels")}
        assert WORK not in channels and PUBLIC in channels
        assert (
            c.execute(
                "SELECT count(*) n FROM api.threads WHERE channel_id=(SELECT id FROM api.channels WHERE name=%s)",
                (WORK,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            c.execute(
                "SELECT api.asker_sees(%s,NULL) ok",
                (
                    owner_rows("SELECT id FROM tl.channels WHERE name=%s", (WORK,))[0][
                        "id"
                    ],
                ),
            ).fetchone()["ok"]
            is False
        )
    # Search and the agent's validated SQL agree.
    hits = ok(
        world["client"].get("/v1/search", params={"q": "work"}, headers=world["agent"])
    )
    assert not leaks(hits)
    rows = ok(
        world["client"].post(
            "/v1/sql",
            json={
                "statement": "SELECT count(*) AS n FROM api.messages WHERE content LIKE '%work%'"
            },
            headers=world["agent"],
        )
    )["rows"]
    assert rows == [{"n": 0}]
    # A grant that allows the label reveals the channel.
    access.grant(GUEST, channel=WORK, labels=["work"], purpose="synthetic work")
    with as_asker() as c:
        seen = {r["id"] for r in c.execute(VISIBLE_MESSAGES)}
        assert {m["wrk-0"], m["wrk-1"]} <= seen
        assert WORK in {r["name"] for r in c.execute("SELECT name FROM api.channels")}


def test_p9_grant_with_label_reveals(world):
    m = world["m"]
    private = public_ids(world, "prv-0", "prv-1", "prv-2")
    seen = ids(VISIBLE_MESSAGES)
    assert not set(private) & set(seen) and m["med"] not in seen
    access.grant(GUEST, channel=PUBLIC, labels=["intimate"])
    seen = ids(VISIBLE_MESSAGES)
    assert set(private) <= set(seen) and m["med"] not in seen
    access.grant(GUEST, channel=PUBLIC, labels=["intimate", "medical"])
    seen = ids(VISIBLE_MESSAGES)
    assert set(private) <= set(seen) and m["med"] in seen
    # Labels are validated.
    with pytest.raises(ValueError):
        access.grant(GUEST, channel=PUBLIC, labels=["no_such_label"])
    with connect() as c, pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute(
            "UPDATE tl.asker_grants SET labels='{no_such_label}' WHERE asker=%s",
            (GUEST,),
        )
    # Narrowing again hides them again.
    access.grant(GUEST, channel=PUBLIC)
    seen = ids(VISIBLE_MESSAGES)
    assert not set(private) & set(seen) and m["med"] not in seen


def test_p9_thread_label_narrows_channel_grant(world):
    private_thread = world["threads"]["private"]
    with as_asker() as c:
        listed = {r["id"] for r in c.execute("SELECT id FROM api.threads")}
        assert private_thread not in listed and world["threads"]["open"] in listed
        assert (
            c.execute(
                "SELECT count(*) n FROM api.conversations WHERE thread_id=%s",
                (private_thread,),
            ).fetchone()["n"]
            == 0
        )
    # A thread grant without the label does not open it either.
    access.grant(GUEST, thread_id=private_thread)
    assert private_thread not in ids("SELECT id FROM api.threads")
    # The most permissive matching grant decides: the thread grant that
    # allows the label reveals the thread although the channel grant does not.
    access.grant(GUEST, thread_id=private_thread, labels=["intimate"])
    assert private_thread in ids("SELECT id FROM api.threads")
    private = public_ids(world, "prv-0", "prv-1", "prv-2")
    assert set(private) <= set(ids(VISIBLE_MESSAGES))
    # Channel labels apply to thread grants as well.
    work_thread = world["threads"]["w"]
    access.grant(GUEST, thread_id=work_thread, labels=["intimate"])
    assert work_thread not in ids("SELECT id FROM api.threads")


def test_p9_message_access_label_applies_to_history(world):
    m = world["m"]
    seen = ids(VISIBLE_MESSAGES)
    assert m["med"] not in seen and m["custom"] in seen
    before = access.policy_version()
    access.add_label_kind("synthetic_custom", "Synthetic tag", purpose="synthetic")
    # The stored message is labelled now, without a re-import.
    assert access.policy_version() > before
    assert m["custom"] not in ids(VISIBLE_MESSAGES)
    with as_asker() as c:
        assert (
            c.execute("SELECT api.message_visible(%s) ok", (m["custom"],)).fetchone()[
                "ok"
            ]
            is False
        )
        assert (
            c.execute("SELECT api.current_message(%s) id", (m["custom"],)).fetchone()[
                "id"
            ]
            is None
        )
    access.grant(GUEST, channel=PUBLIC, labels=["synthetic_custom"])
    assert m["custom"] in ids(VISIBLE_MESSAGES) and m["med"] not in ids(
        VISIBLE_MESSAGES
    )
    # Records and appearances follow the same row rule.
    with as_asker() as c:
        records = {
            r["message_id"] for r in c.execute("SELECT message_id FROM api.records")
        }
    assert m["custom"] in records and m["med"] not in records
    with pytest.raises(ValueError):
        access.add_label_kind("Not A Label")


def test_p9_page_citing_labelled_message_hidden(world):
    m = world["m"]
    page_records = "SELECT message_id AS id FROM api.records WHERE message_id=%s"
    assert ids(page_records, params=(m["page"],)) == []
    assert m["page"] not in ids(VISIBLE_MESSAGES)
    recall = ok(
        world["client"].post(
            "/v1/recall",
            json={
                "query": "orchard page",
                "k": 50,
                "scope": {"kinds": ["page_revision", "passage"]},
            },
            headers=world["agent"],
        )
    )
    assert all(r.get("message_id") != m["page"] for r in recall["records"])
    assert not leaks(recall)
    # Allowing the cited message's label makes the page readable.
    access.grant(GUEST, channel=PUBLIC, labels=["medical"])
    assert ids(page_records, params=(m["page"],)) == [m["page"]]
    access.grant(GUEST, channel=PUBLIC)
    assert ids(page_records, params=(m["page"],)) == []


# --- expiry and the policy version ------------------------------------------------


def test_p9_grant_expiry_hides_rows_and_invalidates_sets_and_investigations(world):
    client, agent = world["client"], world["agent"]
    expires = datetime.now(timezone.utc) + timedelta(seconds=3)
    access.grant(GUEST, channel=PUBLIC, purpose="synthetic short", expires_at=expires)
    assert ids(VISIBLE_MESSAGES)
    recall = ok(
        client.post(
            "/v1/recall",
            json={
                "query": "orchard",
                "k": 5,
                "save": True,
                "scope": {"channels": [PUBLIC]},
            },
            headers=agent,
        )
    )
    handle = recall["result_set"]
    assert ok(client.get(f"/v1/sets/{handle}", headers=agent))["records"]
    investigation = None
    if has_route(client.app, "POST", "/v1/librarian"):
        packet = ok(
            client.post("/v1/librarian", json={"goal": "orchard"}, headers=agent)
        )
        investigation = packet.get("investigation_id")
        assert investigation, packet
        if investigation:
            assert (
                client.get(
                    f"/v1/investigations/{investigation}", headers=agent
                ).status_code
                == 200
            )
    version = access.policy_version()
    wait = (expires - datetime.now(timezone.utc)).total_seconds() + 0.3
    time.sleep(max(wait, 0))
    # Nothing was written; the expiry alone moved the version by one.
    assert access.policy_version() == version + 1
    with as_asker() as c:
        public = {
            r["id"]
            for r in c.execute(
                "SELECT m.id FROM api.messages m WHERE m.channel=%s", (PUBLIC,)
            )
        }
        assert public == set()
        described = c.execute("SELECT api.describe_access() d").fetchone()["d"]
    assert PUBLIC not in described["granted_channels"]
    (expired,) = [g for g in described["grants"] if g["channel"] == PUBLIC]
    assert expired["expired"] is True and expired["purpose"] == "synthetic short"
    assert client.get(f"/v1/sets/{handle}", headers=agent).status_code == 409
    if investigation:
        assert (
            client.get(f"/v1/investigations/{investigation}", headers=agent).status_code
            == 409
        )
    (row,) = [g for g in access.list_grants(GUEST) if g["channel"] == PUBLIC]
    assert row["expired"] is True


def test_p9_policy_version_monotonic_when_expired_grant_extended_or_deleted(world):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    versions = [access.policy_version()]

    def step(action):
        action()
        versions.append(access.policy_version())
        assert versions[-1] > versions[-2], versions

    step(lambda: access.grant(GUEST, channel=PUBLIC, expires_at=past))
    # Extending an expired grant lowers the expired count by one; the trigger
    # compensates, so the version still moves forward.
    step(lambda: access.extend(GUEST, future, channel=PUBLIC))
    step(lambda: access.extend(GUEST, past, channel=PUBLIC))
    step(lambda: access.revoke(GUEST, channel=PUBLIC))
    step(lambda: access.grant(GUEST, channel=PUBLIC, expires_at=past))
    step(lambda: access.extend(GUEST, None, channel=PUBLIC))
    step(lambda: access.grant(GUEST, channel=PUBLIC, expires_at=past))
    # Direct owner SQL over several expired grants at once.
    with connect() as c:
        c.execute(
            "UPDATE tl.asker_grants SET expires_at=%s WHERE asker=%s",
            (past, GUEST),
        )
    versions.append(access.policy_version())
    assert versions[-1] > versions[-2]
    with connect() as c:
        expired = c.execute(
            "SELECT count(*) n FROM tl.asker_grants WHERE expires_at<=now()"
        ).fetchone()["n"]
    assert expired == 3

    def owner_sql(statement):
        def run():
            with connect() as c:
                c.execute(statement, (GUEST,))

        return run

    step(owner_sql("UPDATE tl.asker_grants SET expires_at=NULL WHERE asker=%s"))
    step(
        owner_sql(
            "UPDATE tl.asker_grants SET expires_at=now()-interval '1 minute' WHERE asker=%s"
        )
    )
    step(owner_sql("DELETE FROM tl.asker_grants WHERE asker=%s"))
    # Unchanged repeats write nothing and leave the version alone.
    access.grant(GUEST, channel=PUBLIC, labels=["medical"])
    version = access.policy_version()
    access.grant(GUEST, channel=PUBLIC, labels=["medical"])
    access.extend(GUEST, None, channel=PUBLIC)
    assert access.policy_version() == version
    for role in ("app", "reader"):
        with connect(role) as c:
            assert c.execute("SELECT api.policy_version() v").fetchone()["v"] == version


# --- audit --------------------------------------------------------------------------


def test_p9_purpose_and_changes_audited(world):
    thread = world["threads"]["open"]
    access.grant(
        GUEST,
        thread_id=thread,
        labels=["medical"],
        purpose="synthetic care visit",
        expires_at="2999-01-01T00:00:00Z",
        actor="synthetic-owner",
    )
    access.extend(
        GUEST, "2999-06-01T00:00:00+00:00", thread_id=thread, purpose="synthetic longer"
    )
    access.label("financial", thread_id=thread, purpose="synthetic money talk")
    access.unlabel("financial", thread_id=thread, purpose="synthetic mistake")
    access.revoke(GUEST, thread_id=thread, purpose="synthetic visit over")
    events = list(reversed(access.access_events()))
    ops = [e["op"] for e in events]
    # The fixture wrote two labels, the asker and three grants first.
    assert ops[:6] == ["label", "label", "asker", "grant", "grant", "grant"]
    assert ops[6:] == ["grant", "extend", "label", "unlabel", "revoke"]
    assert events[2]["detail"] == {"action": "register", "mode": "restricted"}
    events = events[1:]
    grant_event = events[5]
    assert grant_event["actor"] == "synthetic-owner" and grant_event["asker"] == GUEST
    assert grant_event["detail"]["purpose"] == "synthetic care visit"
    assert grant_event["detail"]["labels"] == ["medical"]
    assert grant_event["detail"]["thread_id"] == thread
    assert events[6]["detail"]["purpose"] == "synthetic longer"
    assert events[6]["detail"]["previous_expires_at"].startswith("2999-01-01")
    assert events[7]["detail"] == {
        "thread_id": thread,
        "label": "financial",
        "purpose": "synthetic money talk",
    }
    assert events[9]["detail"]["purpose"] == "synthetic visit over"
    (held,) = [g for g in access.list_grants(GUEST) if g["channel"] == PUBLIC]
    assert held["purpose"] == "synthetic household agent"
    assert held["created_by"] == "owner"
    # Append-only, and the runtime roles cannot write it or any label table.
    with connect() as c:
        for statement in (
            "UPDATE tl.access_events SET actor='x'",
            "DELETE FROM tl.access_events",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(statement)
    for role in ("app", "reader"):
        for statement in (
            "INSERT INTO tl.access_events(op,actor) VALUES('grant','intruder')",
            "INSERT INTO tl.labels(label) VALUES('intruder')",
            "DELETE FROM tl.channel_labels",
            "DELETE FROM tl.thread_labels",
            "UPDATE tl.asker_grants SET labels='{work}'",
        ):
            with pytest.raises(psycopg.Error):
                with connect(role) as c:
                    c.execute(statement)
    with connect("app") as c:
        assert (
            c.execute("SELECT count(*) n FROM tl.access_events").fetchone()["n"] == 11
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect("reader") as c:
            c.execute("SELECT * FROM tl.access_events")


def test_cli_label_and_grant_commands(world):
    from throughline.cli import app

    runner = CliRunner()

    def run(*args, ok=True):
        result = runner.invoke(app, ["lib", *args], color=False)
        assert (result.exit_code == 0) is ok, result.output
        return json.loads(result.output) if ok and "--json" in args else result.output

    labelled = run("access", "label", "financial", "--channel", PUBLIC, "--json")
    assert labelled == {"channel": PUBLIC, "labels": ["financial"]}
    kinds = {r["label"]: r for r in run("access", "labels", "--json")}
    assert SEEDED <= set(kinds) and kinds["financial"]["channels"] == [PUBLIC]
    run("access", "label", "nonexistent", "--channel", PUBLIC, ok=False)
    granted = run(
        "asker",
        "grant",
        GUEST,
        "--channel",
        PUBLIC,
        "--labels",
        "financial,medical",
        "--purpose",
        "synthetic cli",
        "--expires",
        "2999-01-01T00:00:00Z",
        "--json",
    )
    assert PUBLIC in granted["channels"]
    (row,) = [
        g
        for g in run("access", "grants", "--asker", GUEST, "--json")
        if g["channel"] == PUBLIC
    ]
    assert (
        row["labels"] == ["financial", "medical"] and row["purpose"] == "synthetic cli"
    )
    # A forgotten --expires never makes an expiring grant permanent.
    run("access", "extend", GUEST, "--channel", PUBLIC, ok=False)
    (row,) = [g for g in access.list_grants(GUEST) if g["channel"] == PUBLIC]
    assert row["expires_at"] is not None
    run("access", "extend", GUEST, "--channel", PUBLIC, "--expires", "never", "--json")
    (row,) = [g for g in access.list_grants(GUEST) if g["channel"] == PUBLIC]
    assert row["expires_at"] is None
    run("access", "unlabel", "financial", "--channel", PUBLIC, "--json")
    run("access", "unlabel", "financial", "--channel", PUBLIC, ok=False)
    run("access", "extend", GUEST, "--thread", "987654321", ok=False)
    run("access", "grant", GUEST, "--channel", PUBLIC, "--expires", "soon", ok=False)
    assert (
        run("access", "kind", "synthetic_kind", "--description", "Synthetic", "--json")[
            "label"
        ]
        == "synthetic_kind"
    )
    events = run("access", "events", "--json")
    assert {
        e["actor"] for e in events if e["op"] in ("grant", "regrant", "extend")
    } >= {"cli"}


def test_labels_survive_truncation_via_seed_hook(database):
    with connect() as c:
        c.execute("TRUNCATE tl.labels CASCADE")
        assert c.execute("SELECT count(*) n FROM tl.labels").fetchone()["n"] == 0
        truncate_tl(c)
        labels = {r["label"] for r in c.execute("SELECT label FROM tl.labels")}
        # Re-seeding an empty database does not move the policy version.
        version = c.execute("SELECT version FROM tl.policy_state").fetchone()["version"]
    assert labels == SEEDED
    assert version == 1
    with connect() as c:
        hook = c.execute(
            "SELECT statement FROM tl.seed_hooks WHERE name='labels'"
        ).fetchone()
        assert hook and "ON CONFLICT" in hook["statement"]
        c.execute("SELECT tl.reseed()")
        assert c.execute("SELECT count(*) n FROM tl.labels").fetchone()["n"] == len(
            SEEDED
        )


# --- restricted inference -------------------------------------------------------------


def test_restricted_inference_counts_exclude_labelled_rows(client):
    from test_v11_people import CH, SECRET, World, headers, owner_me, q, run

    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    medical = w.say("bm", "dm-bob", "bob-acct", 1100, conversation_kind="dm")
    medical["access_label"] = "medical"
    w.load(
        # Unlabelled rows of the granted channel: alice twice, bob once.
        w.say("a1", "dm-alice", "alice-acct", 0, conversation_kind="dm"),
        owner_me(w, "o1", "dm-alice", 60, conversation_kind="dm"),
        w.say(
            "a2",
            "dm-alice",
            "alice-acct",
            120,
            "Synthetic can you water it?",
            conversation_kind="dm",
        ),
        w.say("b1", "dm-bob", "bob-acct", 1000, conversation_kind="dm"),
        owner_me(w, "o2", "dm-bob", 1060, conversation_kind="dm"),
        medical,
        # A channel labelled 'work' (granted, label not allowed): much more
        # with bob.
        *[
            item
            for i in range(4)
            for item in (
                w.say(
                    f"hb{i}",
                    "secret-bob",
                    "bob-acct",
                    5000 + 120 * i,
                    channel=SECRET,
                    conversation_kind="dm",
                ),
                owner_me(
                    w,
                    f"ho{i}",
                    "secret-bob",
                    5060 + 120 * i,
                    channel=SECRET,
                    conversation_kind="dm",
                ),
            )
        ],
        # A thread labelled 'intimate' inside the granted channel.
        w.say("hq", "secret-alice", "alice-acct", 9000, "Synthetic secret question?"),
        owner_me(w, "hq0", "secret-alice", 8000),
    )
    w.attach("alice-acct", w.alice)
    w.attach("bob-acct", w.bob)
    w.index()
    thread = owner_rows(
        "SELECT t.id FROM tl.threads t JOIN tl.channels c ON c.id=t.channel_id WHERE c.name=%s AND t.external_id='secret-alice'",
        (CH,),
    )[0]["id"]
    access.label("intimate", thread_id=thread)
    access.label("work", channel=SECRET)
    asker = "synthetic-asker"
    access.register_asker(asker)
    access.grant(asker, channel=CH)
    access.grant(asker, channel=SECRET)
    hidden = [w.id(k) for k in ("bm", "hq", "hq0")] + [
        w.id(f"{p}{i}") for p in ("hb", "ho") for i in range(4)
    ]
    visible = [w.id(k) for k in ("a1", "o1", "a2", "b1", "o2")]
    # SQL counts.
    assert q("SELECT count(*) n FROM api.messages", asker=asker) == [{"n": 5}]
    assert q("SELECT count(*) n FROM api.records", asker=asker) == [{"n": 5}]
    assert q("SELECT count(*) n FROM api.appearances", asker=asker) == [{"n": 5}]
    assert q(
        "SELECT count(*) n FROM api.episodes_of(%s::bigint[])", (hidden,), asker=asker
    ) == [{"n": 0}]
    assert q(
        "SELECT count(*) n FROM api.messages WHERE id=ANY(%s)", (visible,), asker=asker
    ) == [{"n": 5}]
    # Person circle and rhythm: as if the labelled rows did not exist.
    seen = {
        r["other_person_id"]: (r["strength"], r["rank"])
        for r in run("circle", asker=asker)
    }
    assert seen == {w.alice: (3, 1), w.bob: (2, 2)}
    full = {r["other_person_id"]: r["rank"] for r in run("circle")}
    assert full[w.bob] == 1
    rhythm = run("rhythm", asker=asker, person_id=w.bob, tz="UTC")
    assert sum(r["messages"] for r in rhythm if r["dimension"] == "month") == 1
    brief = run("person_brief", asker=asker, person_id=w.bob)[0]
    assert brief["message_count"] == 1
    assert [c["channel"] for c in brief["coverage"]] == [CH]
    loops = dict(older_than="1 second", horizon_days=100000)
    assert {r["message_id"] for r in run("open_loops", asker=asker, **loops)} == {
        w.id("a2")
    }
    (status,) = run("exchange_index_status", asker=asker)
    assert status["indexed_threads"] == 2
    # Recall manifests.
    agent = headers("agent", [asker])
    recall = ok(
        client.post(
            "/v1/recall",
            json={"query": "synthetic", "k": 50, "group_by": "episode"},
            headers=agent,
        )
    )
    assert {r["message_id"] for r in recall["records"]} <= set(visible)
    text = json.dumps(recall, default=str)
    assert "secret-alice" not in text and "secret-bob" not in text
    assert "secret question" not in text.lower()
    counts = recall["execution"].get("counts") or {}
    assert (counts.get("retrieved") or 0) <= len(visible)
    # Error probes through validated SQL raise nothing.
    for statement in (
        f"SELECT * FROM api.appearances WHERE 1/(message_id-{w.id('hq')})=1",
        f"SELECT * FROM api.messages WHERE 1/(id-{w.id('bm')})=1",
        f"SELECT * FROM api.person_timeline({w.bob}) WHERE 1/(message_id-{w.id('hb0')})=1",
    ):
        response = client.post("/v1/sql", json={"statement": statement}, headers=agent)
        assert response.status_code == 200, (statement, response.text)
    # Coverage of the labelled channel is not readable.
    if not has_route(client.app, "POST", "/v1/plugins/{name}/coverage"):
        pytest.skip("optional-feature: coverage (source coverage) missing")
    plugin = create_token("plugin", kind="plugin", plugin="synthetic")["token"]
    ok(
        client.post(
            "/v1/plugins/synthetic/coverage",
            headers={"Authorization": "Bearer " + plugin},
            json={
                "reports": [
                    {"channel": CH, "status": "complete"},
                    {"channel": SECRET, "status": "complete"},
                ]
            },
        )
    )
    coverage = json.dumps(ok(client.get("/v1/coverage", headers=agent)))
    assert CH in coverage and SECRET not in coverage
    assert SECRET in json.dumps(ok(client.get("/v1/coverage")))
    # Pending counts of the asker's consumer exclude labelled rows.
    if not has_route(client.app, "GET", "/v1/consumers/{name}/pending"):
        pytest.skip("optional-feature: consumers (source coverage) missing")
    ok(client.put("/v1/consumers/settings", json={"settle_s": 2, "margin_s": 1}))
    ok(
        client.post(
            "/v1/consumers",
            json={"name": "labelled", "start": "beginning"},
            headers=agent,
        )
    )
    ok(client.post("/v1/consumers", json={"name": "everything", "start": "beginning"}))
    time.sleep(2.2)
    pending = ok(client.get("/v1/consumers/labelled/pending", headers=agent))["pending"]
    everything = ok(client.get("/v1/consumers/everything/pending"))["pending"]
    assert 0 < pending <= len(visible) < everything
    news = ok(
        client.get("/v1/consumers/labelled/news", params={"max": 100}, headers=agent)
    )
    assert {r["message_id"] for r in news["rows"]} <= set(visible)


# --- error oracle, owner scope, describe -----------------------------------------------


def test_acl_8_error_oracle_closed_on_labelled_rows(world):
    client, agent = world["client"], world["agent"]
    views = [
        r["relname"]
        for r in owner_rows(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN tl.gates g ON g.view_name=c.relname WHERE n.nspname='api' AND c.relkind='v' ORDER BY 1"
        )
    ]
    assert "messages" in views or "message_versions" in views
    owner_errors = set()
    for view in views + ["messages", "records"]:
        probe = (
            f"SELECT count(*) AS n FROM api.{view} v "
            f"WHERE CASE WHEN lower((v.*)::text) LIKE '%{CANARY}%' "
            f"THEN 1/(0*length((v.*)::text)) ELSE 0 END = 1"
        )
        r = client.post("/v1/sql", json={"statement": probe}, headers=agent)
        assert r.status_code == 200, (view, r.text[:300])
        assert r.json()["rows"] == [{"n": 0}], view
        cast = (
            f"SELECT count(*) AS n FROM api.{view} v WHERE (CASE WHEN lower((v.*)::text) "
            f"LIKE '%{CANARY}%' THEN (v.*)::text ELSE '1' END)::int = 1"
        )
        assert (
            client.post("/v1/sql", json={"statement": cast}, headers=agent).status_code
            == 200
        ), view
        if client.post("/v1/sql", json={"statement": probe}).status_code != 200:
            owner_errors.add(view)
    # The probe is live: for the owner it trips on the labelled rows.
    assert {"messages", "records"} <= owner_errors
    # Search functions and keyword search inside the granted channel.
    for statement in (
        f"SELECT * FROM api.search('{CANARY}', 50) s WHERE 1/(0*length(s.content))=1",
        f"SELECT count(*) AS n FROM api.messages WHERE content LIKE '%{CANARY}%'",
    ):
        r = client.post("/v1/sql", json={"statement": statement}, headers=agent)
        assert r.status_code == 200, (statement, r.text[:300])
        assert not leaks(r.json())


def test_owner_scope_unaffected(world):
    m = world["m"]
    every = sorted(m.values())
    with connect("reader") as c:
        seen = sorted(r["id"] for r in c.execute("SELECT id FROM api.messages"))
    assert seen == every
    for scope in (dict(mode="owner"), dict()):
        with reader(**scope) as c:
            assert (
                sorted(r["id"] for r in c.execute("SELECT id FROM api.messages"))
                == every
            )
            assert {r["name"] for r in c.execute("SELECT name FROM api.channels")} >= {
                PUBLIC,
                WORK,
                PAGE,
            }
    access.register_asker("boss", "owner")
    with as_asker("boss") as c:
        assert (
            sorted(r["id"] for r in c.execute("SELECT id FROM api.messages")) == every
        )
        assert (
            c.execute(
                "SELECT count(*) n FROM api.records WHERE message_id=%s", (m["page"],)
            ).fetchone()["n"]
            == 1
        )
    hits = ok(world["client"].get("/v1/search", params={"q": CANARY}))
    assert leaks(hits)


def test_describe_access_shows_own_labels_only(world):
    private_thread = world["threads"]["private"]
    access.grant(
        GUEST,
        thread_id=private_thread,
        labels=["intimate"],
        purpose="synthetic thread",
        expires_at="2999-01-01T00:00:00Z",
    )
    access.register_asker("rival")
    access.grant("rival", channel=PUBLIC, labels=["financial"], purpose="rival purpose")
    with as_asker() as c:
        mine = c.execute("SELECT api.describe_access() d").fetchone()["d"]
    assert mine["granted_channels"] == sorted([PUBLIC, WORK, PAGE])
    assert mine["granted_threads"] == 1
    grants = {(g["channel"], g["thread_id"]): g for g in mine["grants"]}
    assert grants[(WORK, None)]["target_labels"] == ["work"]
    assert grants[(WORK, None)]["allowed_labels"] == []
    assert grants[(PUBLIC, None)]["target_labels"] == []
    assert grants[(PUBLIC, None)]["purpose"] == "synthetic household agent"
    thread = grants[(None, private_thread)]
    assert thread["target_labels"] == ["intimate"] and thread["allowed_labels"] == [
        "intimate"
    ]
    assert thread["purpose"] == "synthetic thread" and thread["expired"] is False
    assert thread["expires_at"].startswith("2999-01-01")
    text = json.dumps(mine)
    assert "rival" not in text and "financial" not in text
    assert "Labelled content is denied by default" in mine["rule"]
    with as_asker("rival") as c:
        theirs = c.execute("SELECT api.describe_access() d").fetchone()["d"]
    assert [g["channel"] for g in theirs["grants"]] == [PUBLIC]
    assert WORK not in json.dumps(theirs) and "synthetic household" not in json.dumps(
        theirs
    )
    with reader() as c:
        direct = c.execute("SELECT api.describe_access() d").fetchone()["d"]
    assert direct["grants"] == [] and set(direct) == set(mine)
    with connect() as c:
        section = c.execute(
            "SELECT body FROM tl.catalog_sections WHERE subject='disclosure'"
        ).fetchone()
    assert section and "default_deny" in section["body"]


# --- checker additions ---------------------------------------------------------------


def test_p9_policy_version_monotonic_when_expired_grants_truncated(world):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    access.grant(GUEST, channel=PUBLIC, expires_at=past)
    access.grant(GUEST, channel=WORK, expires_at=past)
    before = access.policy_version()
    with connect() as c:
        c.execute("TRUNCATE tl.asker_grants")
    # Two expired grants leave the count; the version still moves forward.
    assert access.policy_version() > before


def test_label_kind_rename_and_removal_fail_closed(world):
    m = world["m"]
    assert m["med"] not in ids(VISIBLE_MESSAGES)
    with connect() as c:
        for statement in (
            "UPDATE tl.labels SET label='health' WHERE label='medical'",
            "DELETE FROM tl.labels WHERE label='medical'",
        ):
            with pytest.raises(psycopg.errors.IntegrityError):
                with c.transaction():
                    c.execute(statement)
        # A kind no stored message names (and no target carries) may go.
        c.execute("DELETE FROM tl.labels WHERE label='financial'")
        c.execute("UPDATE tl.labels SET description='Synthetic' WHERE label='medical'")
    # The labelled history stays hidden.
    assert m["med"] not in ids(VISIBLE_MESSAGES)


def test_asker_registration_and_removal_audited(world):
    access.register_asker(GUEST, "owner", actor="synthetic-owner")
    access.register_asker(GUEST, "owner")
    access.remove_asker(GUEST, actor="synthetic-owner")
    events = [e for e in access.access_events(GUEST) if e["op"] == "asker"]
    assert [e["detail"]["action"] for e in reversed(events)] == [
        "register",
        "mode",
        "remove",
    ]
    assert events[0]["detail"]["grants_removed"] == 3
    assert events[1]["detail"]["previous_mode"] == "restricted"
    assert events[0]["actor"] == "synthetic-owner"


def test_describe_access_thread_grant_shows_channel_labels(world):
    work_thread = world["threads"]["w"]
    access.grant(GUEST, thread_id=work_thread, labels=["work"])
    with as_asker() as c:
        mine = c.execute("SELECT api.describe_access() d").fetchone()["d"]
    (thread,) = [g for g in mine["grants"] if g["thread_id"] == work_thread]
    assert thread["target_labels"] == ["work"]
    assert work_thread in ids("SELECT id FROM api.threads")


def test_link_types_hidden_when_every_grant_expired(world):
    """0042 listed active link-type names to an asker holding any grant;
    an asker whose only grants have expired now sees none, like an asker
    with no grant."""
    access.register_asker("lapsed")
    access.grant("lapsed", channel=PUBLIC)
    statement = "SELECT type AS id FROM api.link_types"
    active = ids(statement, asker="lapsed")
    assert active, "the seeded vocabulary is listed under an active grant"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    access.grant("lapsed", channel=PUBLIC, expires_at=past)
    assert ids(statement, asker="lapsed") == []
    access.extend("lapsed", channel=PUBLIC, expires_at=None)
    assert ids(statement, asker="lapsed") == active


def test_p9_one_grant_must_cover_every_label_of_the_row(world):
    """The grant that admits a row must itself allow all of its labels: a
    channel grant allowing 'medical' and a thread grant allowing 'intimate'
    do not add up to a medical message inside the intimate thread."""
    client = world["client"]
    r = ingest(
        client,
        [
            message(
                700,
                channel=PUBLIC,
                external_id="prv-med",
                thread={"external_id": "private"},
                content=f"Synthetic orchard {CANARY} private medical",
                fields={},
                access_label="medical",
            )
        ],
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    both = message_id(PUBLIC, "prv-med")
    private = public_ids(world, "prv-0", "prv-1", "prv-2")
    access.grant(GUEST, channel=PUBLIC, labels=["medical"])
    access.grant(GUEST, thread_id=world["threads"]["private"], labels=["intimate"])
    seen = set(ids(VISIBLE_MESSAGES))
    assert world["m"]["med"] in seen and set(private) <= seen
    assert both not in seen
    with as_asker() as c:
        assert (
            c.execute("SELECT api.message_visible(%s) ok", (both,)).fetchone()["ok"]
            is False
        )
    # One grant allowing both labels admits it.
    access.grant(
        GUEST, thread_id=world["threads"]["private"], labels=["intimate", "medical"]
    )
    assert both in ids(VISIBLE_MESSAGES)


def test_label_kind_description_change_audited(world):
    access.add_label_kind("medical", "Synthetic health", purpose="synthetic wording")
    (event,) = [
        e
        for e in access.access_events()
        if e["op"] == "label_kind" and e["detail"].get("label") == "medical"
    ]
    assert event["detail"]["description"] == "Synthetic health"
    assert event["detail"]["previous_description"] == "Health and medical content"
    assert event["detail"]["purpose"] == "synthetic wording"
    # An unchanged description writes nothing.
    access.add_label_kind("medical", "Synthetic health")
    assert len([e for e in access.access_events() if e["op"] == "label_kind"]) == 1


def test_p9_mixed_conversation_not_listed_or_ranked_for_restricted_asker(world):
    """A conversation chunk holding a message whose own label is denied is
    built from that message's text too: a restricted asker neither lists it
    (token_estimate, text_hash) nor gets it ranked by the meaning legs, so a
    visible neighbour never ranks for words only the hidden message holds.
    Its visible messages are still found lexically."""
    m = world["m"]
    open_thread = world["threads"]["open"]
    with connect() as c:
        c.execute("SELECT tl.regroup_conversations(NULL)")
        c.execute(
            "INSERT INTO tl.embedding_models(model,dimensions,active) VALUES('synthetic-mixed',3,true) ON CONFLICT (model) DO UPDATE SET active=true"
        )
        c.execute(
            "INSERT INTO tl.embeddings(conversation_id,model,text_hash,embedding) SELECT id,'synthetic-mixed',text_hash,'[1,1,1]'::vector FROM tl.conversations WHERE text_hash IS NOT NULL ON CONFLICT DO NOTHING"
        )
    chunks = owner_rows(
        "SELECT c.id,EXISTS(SELECT 1 FROM tl.messages x WHERE x.id=%s AND x.thread_id=c.thread_id AND x.thread_no BETWEEN c.first_thread_no AND c.last_thread_no) AS mixed FROM tl.conversations c WHERE c.thread_id=%s",
        (m["med"], open_thread),
    )
    mixed = {r["id"] for r in chunks if r["mixed"]}
    clean = {r["id"] for r in chunks if not r["mixed"]}
    assert mixed, "the medical message belongs to a chunk of the open thread"
    vectors = (
        "SELECT DISTINCT conversation_id AS id FROM api.vector_records('[1,1,1]',100)"
    )
    meaning = (
        "SELECT DISTINCT conversation_id AS id FROM api.search_meaning('[1,1,1]',100)"
    )
    listed = "SELECT id FROM api.conversations WHERE thread_id=%s"

    def seen(statement, params=None):
        return set(ids(statement, params=params))

    assert not seen(listed, (open_thread,)) & mixed
    assert not seen(vectors) & mixed and not seen(meaning) & mixed
    assert seen(listed, (open_thread,)) == clean
    # The visible neighbours of the hidden message are still found by words.
    neighbours = public_ids(world, *OPEN)
    with as_asker() as c:
        lexical = {
            r["message_id"]
            for r in c.execute(
                "SELECT message_id FROM api.search_records('orchard',100)"
            )
        }
    assert set(neighbours) <= lexical and m["med"] not in lexical
    # The owner is unaffected.
    with connect() as c:
        owned = {
            r["id"]
            for r in c.execute(
                "SELECT id FROM api.conversations WHERE thread_id=%s", (open_thread,)
            )
        }
    assert mixed <= owned
    # A grant allowing the label admits the chunk again.
    access.grant(GUEST, channel=PUBLIC, labels=["medical"])
    assert mixed <= seen(listed, (open_thread,))
    assert mixed <= seen(vectors) and mixed <= seen(meaning)
