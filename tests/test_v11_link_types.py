"""The closed link-type registry (migration 0042, throughline.vocabulary):
total normalisation, synonym folding, method-first writer rules, plugin
candidates and retired types that never stop an import, merge with
duplicate collapse, proposals and the seed hooks. Synthetic data only."""

import re
from pathlib import Path

import psycopg
import pytest
from conftest import message, truncate_tl

from throughline import access
from throughline.db import connect, create_token

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "throughline"
    / "migrations"
    / "0042_claims.sql"
)


def headers(kind, plugin=None, askers=None):
    token = create_token("synthetic-" + kind, kind, plugin, askers)["token"]
    return {"Authorization": "Bearer " + token}


def key(external_id, channel="synthetic.chat"):
    return {"channel": channel, "external_id": external_id}


def current(external_id, channel="synthetic.chat"):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


def registry(name):
    with connect() as c:
        return c.execute(
            "SELECT * FROM tl.link_types WHERE type=%s", (name,)
        ).fetchone()


def links(type_=None):
    with connect() as c:
        return c.execute(
            "SELECT * FROM tl.links WHERE (%s::text IS NULL OR type=%s) ORDER BY id",
            (type_, type_),
        ).fetchall()


def link(client, type_, a="fake-0", b="fake-1", headers=None, **extra):
    return client.post(
        "/v1/links",
        json={"type": type_, "from": key(a), "to": key(b), **extra},
        headers=headers or {},
    )


def plugin_batch(client, plugin_headers, link_list, messages=None):
    return client.post(
        "/v1/ingest/messages",
        json={"plugin": "synthetic", "messages": messages or [], "links": link_list},
        headers=plugin_headers,
    )


@pytest.fixture()
def world(client, channel):
    r = client.post(
        "/v1/ingest/messages",
        json={"plugin": "synthetic", "messages": [message(i) for i in range(4)]},
    )
    assert r.status_code == 200, r.text
    return client


@pytest.fixture()
def plugin(world):
    h = headers("plugin", "synthetic")
    r = world.post(
        "/v1/plugins/synthetic/manifest",
        json={"name": "synthetic", "link_types": ["synthetic_listened"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return h


def refused(response):
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "23514", response.text


# --- writer rules ---------------------------------------------------------------


def test_link_vocabulary_agent_free_text_refused(world):
    agent = headers("agent")
    refused(link(world, "synthetic free text", headers=agent))
    assert registry("synthetic_free_text") is None
    assert link(world, "about", headers=agent).status_code == 200
    # A synonym is folded for agents as well.
    r = link(world, "Replies-To", headers=agent)
    assert r.status_code == 200 and r.json()["type"] == "reply_to"


def test_link_vocabulary_rule_and_model_methods_refused_unregistered(world):
    evidence = {"message_ids": [current("fake-2")]}
    for method in ("model", "rule"):
        refused(
            link(
                world,
                "synthetic_guess_" + method,
                method=method,
                evidence=evidence,
                job="synthetic-job",
                job_version="1",
            )
        )
        assert registry("synthetic_guess_" + method) is None
    r = link(
        world, "about", method="model", evidence=evidence, job="j", job_version="1"
    )
    assert r.status_code == 200, r.text


def test_owner_connection_model_link_with_new_type_refused(world):
    a, b = current("fake-0"), current("fake-1")
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        with connect() as c:
            c.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method,evidence,job,job_version) VALUES('synthetic_curated','message',%s,'message',%s,'model',%s,'curator','1')",
                (a, b, psycopg.types.json.Jsonb({"message_ids": [a]})),
            )
    assert "unregistered link type: synthetic_curated" in str(error.value)
    with connect() as c:
        row = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES('synthetic_imported','message',%s,'message',%s) RETURNING *",
            (a, b),
        ).fetchone()
    assert row["type"] == "synthetic_imported"
    assert registry("synthetic_imported")["origin"] == "owner"
    # The app role with no token is no owner connection.
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect("app") as c:
            c.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES('synthetic_app_kind','message',%s,'message',%s)",
                (a, b),
            )


def test_owner_new_type_auto_registered(world):
    r = link(world, "synthetic_owner_kind")
    assert r.status_code == 200, r.text
    row = registry("synthetic_owner_kind")
    assert (row["status"], row["origin"]) == ("active", "owner")
    # Now registered, agents may use it.
    assert (
        link(
            world, "synthetic_owner_kind", "fake-2", "fake-3", headers("agent")
        ).status_code
        == 200
    )


# --- folding and normalisation ---------------------------------------------------


def test_link_vocabulary_synonym_folded_type_as_written_kept(world):
    r = link(world, "Replies-To")
    assert r.status_code == 200, r.text
    assert (r.json()["type"], r.json()["type_as_written"]) == ("reply_to", "Replies-To")
    r = link(world, "partof", "fake-1", "fake-2")
    assert (r.json()["type"], r.json()["type_as_written"]) == ("part_of", "partof")
    # A name that is already canonical keeps no raw copy.
    r = link(world, "about", "fake-2", "fake-3")
    assert r.json()["type_as_written"] is None


def test_link_vocabulary_replay_with_synonym_returns_existing_link(world, plugin):
    body = [{"type": "replies_to", "from": key("fake-0"), "to": key("fake-1")}]
    for _ in range(3):
        r = plugin_batch(world, plugin, body)
        assert r.status_code == 200, r.text
    rows = links("reply_to")
    assert len(rows) == 1 and rows[0]["type_as_written"] == "replies_to"
    first = link(world, "In Reply To", "fake-2", "fake-3").json()
    again = link(world, "in_reply_to", "fake-2", "fake-3").json()
    assert first["id"] == again["id"] and len(links("reply_to")) == 2


def test_normalisation_total(world, plugin):
    with connect() as c:

        def norm(raw):
            return c.execute(
                "SELECT tl.normalize_link_type(%s) AS t", (raw,)
            ).fetchone()["t"]

        symbols, foreign, digit = (
            norm("!!! ???"),
            norm("日本語のリンク"),
            norm("2nd-Order Link"),
        )
        assert re.fullmatch(r"t_[0-9a-f]{12}", symbols)
        assert re.fullmatch(r"t_[0-9a-f]{12}", foreign) and foreign != symbols
        assert digit == "t_2nd_order_link"
        assert norm("  Mixed CASE__with--dashes  ") == "mixed_case_with_dashes"
        long = norm("a" * 100)
        assert len(long) == 63 and long == "a" * 63
        assert len(norm("9" * 100)) == 63
        for raw in ("", " ", "_", "Ä", "x" * 500, "1", "a-b"):
            assert re.fullmatch(r"[a-z][a-z0-9_]{0,62}", norm(raw)), raw
    raws = ["!!! ???", "日本語のリンク", "2nd-Order Link"]
    r = plugin_batch(
        world,
        plugin,
        [
            {"type": raw, "from": key("fake-0"), "to": key(f"fake-{i + 1}")}
            for i, raw in enumerate(raws)
        ],
    )
    assert r.status_code == 200, r.text
    stored = {row["type_as_written"]: row["type"] for row in links()}
    assert stored == {
        "!!! ???": symbols,
        "日本語のリンク": foreign,
        "2nd-Order Link": digit,
    }
    for name in stored.values():
        assert registry(name)["status"] == "candidate"


# --- plugins ----------------------------------------------------------------------


def test_link_vocabulary_plugin_declared_type_registered(world, plugin):
    r = plugin_batch(
        world,
        plugin,
        [{"type": "Synthetic Listened", "from": key("fake-0"), "to": key("fake-1")}],
    )
    assert r.status_code == 200, r.text
    row = registry("synthetic_listened")
    assert (row["status"], row["origin"], row["plugin"]) == (
        "active",
        "plugin",
        "synthetic",
    )
    assert links("synthetic_listened")[0]["type_as_written"] == "Synthetic Listened"


def test_link_vocabulary_plugin_undeclared_type_candidate_not_blocking(world, plugin):
    r = plugin_batch(
        world,
        plugin,
        [{"type": "synthetic_hummed", "from": key("fake-0"), "to": key("fake-1")}],
        messages=[message(10)],
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1
    row = registry("synthetic_hummed")
    assert (row["status"], row["origin"], row["plugin"]) == (
        "candidate",
        "source_candidate",
        "synthetic",
    )
    assert row["first_seen"] is not None
    assert len(links("synthetic_hummed")) == 1
    # Still unregistered for everyone else.
    refused(link(world, "synthetic_hummed", "fake-2", "fake-3", headers("agent")))
    listed = world.get("/v1/link-types", params={"status": "candidate"}).json()["rows"]
    assert [x["type"] for x in listed] == ["synthetic_hummed"]
    owner_view = {
        x["type"]: x["status"] for x in world.get("/v1/link-types").json()["rows"]
    }
    assert owner_view["synthetic_hummed"] == "candidate"
    # The owner registering it makes it active.
    r = world.post(
        "/v1/link-types",
        json={"type": "synthetic_hummed", "description": "Hummed along"},
    )
    assert r.status_code == 200 and r.json()["status"] == "active"


def test_plugin_batch_with_retired_and_symbol_only_types_imports(world, plugin):
    assert (
        world.post("/v1/link-types", json={"type": "synthetic_old"}).status_code == 200
    )
    assert world.post("/v1/link-types/synthetic_old/retire", json={}).status_code == 200
    assert (
        world.post("/v1/link-types", json={"type": "synthetic_gone"}).status_code == 200
    )
    r = world.post("/v1/link-types/synthetic_gone/merge", json={"into": "about"})
    assert r.status_code == 200, r.text
    batch = [
        {"type": "synthetic_old", "from": key("fake-0"), "to": key("fake-1")},
        {"type": "%%%", "from": key("fake-0"), "to": key("fake-2")},
        {"type": "synthetic_gone", "from": key("fake-0"), "to": key("fake-3")},
        {"type": "reply_to", "from": key("fake-1"), "to": key("fake-2")},
    ]
    r = plugin_batch(world, plugin, batch, messages=[message(11)])
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1
    assert len(links("synthetic_old")) == 1
    assert [x["type_as_written"] for x in links("about")] == ["synthetic_gone"]
    old = registry("synthetic_old")
    assert old["status"] == "retired" and old["last_written_while_retired"] is not None
    retired = world.get("/v1/link-types", params={"status": "retired"}).json()["rows"]
    flags = {x["type"]: x["retired_but_written"] for x in retired}
    assert flags == {"synthetic_old": True, "synthetic_gone": False}
    # Agents are still refused for the retired type.
    refused(link(world, "synthetic_old", "fake-2", "fake-3", headers("agent")))


# --- merge and retire --------------------------------------------------------------


def test_link_vocabulary_merge_repoints_with_history_and_folds_future_writes(
    world, plugin
):
    body = [
        {"type": "synthetic_heard", "from": key("fake-0"), "to": key("fake-1")},
        {"type": "synthetic_heard", "from": key("fake-1"), "to": key("fake-2")},
    ]
    assert plugin_batch(world, plugin, body).status_code == 200
    ids = [x["id"] for x in links("synthetic_heard")]
    r = world.post("/v1/link-types/synthetic_heard/merge", json={"into": "consumed"})
    assert r.status_code == 200, r.text
    # Writes now carry a receipt (write contract); compare the result only.
    assert {k: v for k, v in r.json().items() if k != "receipt"} == {
        "type": "synthetic_heard",
        "into": "consumed",
        "retyped": 2,
        "collapsed": 0,
    }
    rows = links("consumed")
    assert [x["id"] for x in rows] == ids
    assert {x["type_as_written"] for x in rows} == {"synthetic_heard"}
    with connect() as c:
        history = c.execute(
            "SELECT link_id,old->>'type' AS type FROM tl.link_history WHERE link_id=ANY(%s) ORDER BY link_id",
            (ids,),
        ).fetchall()
        synonym = c.execute(
            "SELECT * FROM tl.link_type_synonyms WHERE alias='synthetic_heard'"
        ).fetchone()
    assert [h["type"] for h in history] == ["synthetic_heard", "synthetic_heard"]
    assert synonym["type"] == "consumed" and synonym["origin"] == "owner"
    # The candidate is gone; future writes fold, for plugins and agents alike.
    assert registry("synthetic_heard") is None
    assert (
        plugin_batch(
            world,
            plugin,
            [{"type": "synthetic_heard", "from": key("fake-2"), "to": key("fake-3")}],
        ).status_code
        == 200
    )
    r = link(world, "synthetic_heard", "fake-3", "fake-0", headers("agent"))
    assert r.status_code == 200 and r.json()["type"] == "consumed"
    assert len(links("consumed")) == 4 and not links("synthetic_heard")


def test_link_vocabulary_merge_collapses_duplicate_ends(world, plugin):
    first = plugin_batch(
        world, plugin, [{"type": "reply", "from": key("fake-0"), "to": key("fake-1")}]
    )
    assert first.status_code == 200, first.text
    assert registry("reply")["status"] == "candidate"
    plugin_batch(
        world,
        plugin,
        [{"type": "reply_to", "from": key("fake-0"), "to": key("fake-1")}],
    )
    plugin_batch(
        world, plugin, [{"type": "reply", "from": key("fake-2"), "to": key("fake-3")}]
    )
    oldest = links("reply")[0]["id"]
    r = world.post("/v1/link-types/reply/merge", json={"into": "reply_to"})
    assert r.status_code == 200, r.text
    assert (r.json()["retyped"], r.json()["collapsed"]) == (2, 1)
    rows = links("reply_to")
    active = [x for x in rows if x["status"] == "active"]
    assert len(rows) == 3 and len(active) == 2
    pair = [x for x in rows if x["from_id"] == current("fake-0")]
    kept = [x for x in pair if x["status"] == "active"]
    gone = [x for x in pair if x["status"] == "superseded"]
    assert [x["id"] for x in kept] == [oldest]
    assert [x["superseded_by"] for x in gone] == [oldest]


def test_retire_requires_into_for_source_types(world, plugin):
    plugin_batch(
        world,
        plugin,
        [{"type": "synthetic_listened", "from": key("fake-0"), "to": key("fake-1")}],
    )
    plugin_batch(
        world,
        plugin,
        [{"type": "synthetic_whistled", "from": key("fake-0"), "to": key("fake-2")}],
    )
    for name in ("synthetic_listened", "synthetic_whistled"):
        r = world.post(f"/v1/link-types/{name}/retire", json={})
        assert r.status_code == 400 and "into" in r.json()["detail"], r.text
    r = world.post(
        "/v1/link-types/synthetic_whistled/retire", json={"into": "consumed"}
    )
    assert r.status_code == 200 and r.json()["retyped"] == 1
    # An owner type without source links retires plainly.
    world.post("/v1/link-types", json={"type": "synthetic_private"})
    r = world.post("/v1/link-types/synthetic_private/retire", json={})
    assert r.status_code == 200 and registry("synthetic_private")["status"] == "retired"
    # Only the owner changes the registry.
    agent = headers("agent")
    assert (
        world.post(
            "/v1/link-types", json={"type": "x_agent"}, headers=agent
        ).status_code
        == 403
    )
    assert (
        world.post("/v1/link-types/about/retire", json={}, headers=agent).status_code
        == 403
    )


def test_link_vocabulary_retired_type_refused_for_agents(world):
    world.post("/v1/link-types", json={"type": "synthetic_dated"})
    assert (
        world.post("/v1/link-types/synthetic_dated/retire", json={}).status_code == 200
    )
    r = link(world, "synthetic_dated", headers=headers("agent"))
    refused(r)
    refused(link(world, "synthetic_dated", method="rule", job="j", job_version="1"))


def test_link_type_proposal_accept_registers_or_merges(world, plugin):
    agent = headers("agent")
    plugin_batch(
        world,
        plugin,
        [{"type": "synthetic_glanced", "from": key("fake-0"), "to": key("fake-1")}],
    )
    new = world.post(
        "/v1/proposals",
        json={
            "kind": "link_type",
            "payload": {"type": "synthetic_proposed", "description": "Proposed"},
        },
        headers=agent,
    )
    merge = world.post(
        "/v1/proposals",
        json={
            "kind": "link_type",
            "payload": {"type": "synthetic_glanced", "merge_into": "consumed"},
        },
        headers=agent,
    )
    assert new.status_code == 200 and merge.status_code == 200, (new.text, merge.text)
    assert registry("synthetic_proposed") is None
    # Agents cannot decide.
    assert (
        world.post(
            f"/v1/proposals/{new.json()['id']}/decide",
            json={"accept": True},
            headers=agent,
        ).status_code
        == 403
    )
    r = world.post(f"/v1/proposals/{new.json()['id']}/decide", json={"accept": True})
    assert r.status_code == 200 and r.json()["status"] == "accepted", r.text
    assert registry("synthetic_proposed")["status"] == "active"
    r = world.post(f"/v1/proposals/{merge.json()['id']}/decide", json={"accept": True})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["merge"]["retyped"] == 1
    assert links("consumed")[0]["type_as_written"] == "synthetic_glanced"


def test_link_types_view_active_only_for_restricted_and_no_counts(world, plugin):
    plugin_batch(
        world,
        plugin,
        [{"type": "synthetic_peeked", "from": key("fake-0"), "to": key("fake-1")}],
    )
    world.post("/v1/link-types", json={"type": "synthetic_retiring"})
    world.post("/v1/link-types/synthetic_retiring/retire", json={})
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    guest = {"X-Throughline-Asker": "guest"}
    r = world.get("/v1/link-types", headers=guest)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert set(rows[0]) == {
        "type",
        "description",
        "inverse",
        "status",
        "origin",
        "synonyms",
    }
    assert {x["status"] for x in rows} == {"active"}
    names = {x["type"] for x in rows}
    assert "about" in names and not names & {"synthetic_peeked", "synthetic_retiring"}
    reply = next(x for x in rows if x["type"] == "reply_to")
    assert "replies_to" in reply["synonyms"]
    for params in (
        {"with_counts": "true"},
        {"status": "candidate"},
        {"status": "retired"},
    ):
        assert (
            world.get("/v1/link-types", params=params, headers=guest).status_code == 403
        )
    owner = world.get("/v1/link-types", params={"with_counts": "true"}).json()["rows"]
    counts = {x["type"]: x["active_links"] for x in owner}
    assert counts["synthetic_peeked"] == 1 and counts["about"] == 0
    # A restricted asker without any grant sees nothing at all.
    access.register_asker("nobody")
    assert (
        world.get("/v1/link-types", headers={"X-Throughline-Asker": "nobody"}).json()[
            "rows"
        ]
        == []
    )
    with connect("reader") as c:
        c.execute("SET LOCAL throughline.scope_mode='asker'")
        c.execute("SET LOCAL throughline.asker='guest'")
        statuses = c.execute("SELECT DISTINCT status FROM api.link_types").fetchall()
    assert statuses == [{"status": "active"}]


# --- seeds and existing data ----------------------------------------------------------


def _legacy_statements():
    text = MIGRATION.read_text(encoding="utf-8")
    start = text.index(
        "UPDATE tl.links SET type_as_written=type,type=tl.normalize_link_type(type)"
    )
    end = text.index("ON CONFLICT (type) DO NOTHING;", start) + len(
        "ON CONFLICT (type) DO NOTHING;"
    )
    return text[start:end]


def test_existing_link_types_seeded_legacy_and_existing_tests_green(world):
    seeded = {
        "about",
        "reply_to",
        "part_of",
        "cites",
        "mentions",
        "author",
        "recipient",
        "participant",
        "present",
        "at_place",
        "same_as",
        "transcript_of",
        "episode_of",
        "published_by",
        "subscribed_to",
        "consumed",
        "related_to",
        "derived_from",
        "synthetic",
    }
    with connect() as c:
        rows = {
            r["type"]: r for r in c.execute("SELECT * FROM tl.link_types").fetchall()
        }
    assert seeded <= set(rows)
    assert {rows[t]["origin"] for t in seeded} == {"core"}
    assert {rows[t]["status"] for t in seeded} == {"active"}
    a, b = current("fake-0"), current("fake-1")
    # Links stored before the registry (written with triggers off, as before
    # migration 0042) are normalised, folded and registered as legacy.
    with connect() as c:
        c.execute("SET LOCAL session_replication_role='replica'")
        for raw in ("Synthetic Legacy-Kind", "replies_to", "synthetic_legacy_plain"):
            c.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES(%s,'message',%s,'message',%s)",
                (raw, a, b),
            )
    with connect() as c:
        # The gate trigger is created after these statements in the migration.
        c.execute("SET LOCAL session_replication_role='replica'")
        c.execute(_legacy_statements())
        stored = c.execute(
            "SELECT type,type_as_written FROM tl.links ORDER BY id"
        ).fetchall()
    assert stored == [
        {"type": "synthetic_legacy_kind", "type_as_written": "Synthetic Legacy-Kind"},
        {"type": "reply_to", "type_as_written": "replies_to"},
        {"type": "synthetic_legacy_plain", "type_as_written": None},
    ]
    assert registry("synthetic_legacy_kind")["origin"] == "legacy"
    assert registry("synthetic_legacy_plain")["status"] == "active"
    assert registry("reply_to")["origin"] == "core"
    assert registry("replies_to") is None


def test_seed_hooks_restore_registry_after_truncation_and_registrations_do_not_leak(
    world,
):
    assert (
        world.post("/v1/link-types", json={"type": "synthetic_leaky"}).status_code
        == 200
    )
    with connect() as c:
        c.execute("UPDATE tl.link_types SET status='retired' WHERE type='about'")
        hooks = {r["name"] for r in c.execute("SELECT name FROM tl.seed_hooks")}
        assert {"link_types", "link_type_synonyms"} <= hooks
        emptied = truncate_tl(c)
        assert "seed_hooks" not in emptied and "link_types" in emptied
        assert c.execute("SELECT tl.reseed() AS n").fetchone()["n"] >= 2
    assert registry("synthetic_leaky") is None
    assert registry("about")["status"] == "active"
    with connect() as c:
        assert (
            c.execute("SELECT tl.resolve_link_type('Replies To') AS t").fetchone()["t"]
            == "reply_to"
        )
        # Hooks are owner-only.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("app") as app:
                app.execute("SELECT tl.reseed()")
