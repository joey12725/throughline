"""Versioned, execution-tested recipes (INT-9, LIB-3) and their discovery.

Every tested recipe file runs against one synthetic fixture corpus; a recipe
whose needs are absent on this server (a feature of a sibling package not
installed yet) is reported skipped, never failed. Optional-feature checks name
the missing feature when they skip.
"""

import json
import re
from pathlib import Path

import pytest
from conftest import ingest, message
from typer.testing import CliRunner

from throughline import access, recipes
from throughline.db import connect, create_token
from throughline.scope import Scope

SRC = Path(recipes.__file__).resolve().parents[1]
BEHAVIOUR_RECIPES = {
    "coverage_first": ("function:coverage_for",),
    "feed_loop": ("function:news", "function:pending"),
    "incidents_by_episode": ("function:episodes_of",),
    "cite_primary_evidence": ("function:recall",),
    "annotate_not_rewrite": ("function:effective", "route:POST /v1/annotations"),
    "link_type_registry": ("view:link_types", "route:GET /v1/link-types"),
}
CORE = (
    "catalogue_first",
    "recall_then_context",
    "recall_hard_scope",
    "as_of_page_lookup",
    "historically_valid_relationship",
    "match_over_bounded_set",
    "exhaustive_versus_ranked",
    "refine_saved_set",
    "inspect_trace",
)
QRY_12 = (
    "near_place_six_months",
    "day_with_person",
    "near_here_talking_money",
    "scored_above_threshold",
    "playing_when_said",
    "context_around_hit",
)
# People and ops recipes that run on the fixture corpus.
PEOPLE_OPS_RECIPES = {
    "person_brief_first": ("function:person_brief",),
    "last_contact_pitfall": ("function:last_contact",),
    "relationship_arc": ("function:person_arc",),
    "open_loops_both_ways": ("function:open_loops",),
    "between_versus_author": ("function:between",),
    "around_a_time": ("function:around",),
    "circle_and_ties": ("function:circle", "function:ties"),
    "write_with_receipt": ("subject:write_contract",),
    "verify_backup_alarm": ("subject:ops",),
}
# Recipes of optional features (disclosure): they skip,
# naming the missing need, until the feature is installed.
OPTIONAL_FEATURE_RECIPES = {
    "disclose_by_label": ("subject:disclosure",),
}
PROBE = "zz_recipe_probe"


def ok(response):
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def has_route(client, method, path):
    return (method, path) in recipes._app_routes(client.app)


def build_corpus(client):
    """The fixture corpus every recipe is written against (synthetic only)."""
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name="synthetic.chat",
                plugin="synthetic",
                fields=[dict(name="rating", type="integer", description="Synthetic")],
            ),
        )
    )
    ok(
        client.post(
            "/v1/channels", json=dict(name="synthetic.music", plugin="synthetic")
        )
    )
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name="synthetic.page",
                plugin="synthetic",
                is_reference=True,
                record_kind="page_revision",
            ),
        )
    )
    items = []
    for i in range(12):
        content = f"Synthetic orchard message {i}"
        if i in (3, 4):
            content = f"We talked about money near the orchard {i}"
        extra = {"location": {"lat": 1, "lon": 2}} if i < 6 else {}
        items.append(
            message(
                i * 60,
                external_id=f"fake-{i}",
                content=content,
                fields={"rating": i},
                **extra,
            )
        )
    items.append(
        message(
            0,
            channel="synthetic.music",
            external_id="song-1",
            thread={"external_id": "plays"},
            content="Synthetic song playing",
            ended_at="2020-01-01T00:20:00Z",
            fields={},
            author=None,
        )
    )
    page = dict(
        channel="synthetic.page",
        external_id="wiki",
        thread={"external_id": "wiki"},
        fields={},
        author=None,
    )
    items.append(
        message(0, content="# Orchard\n\nFirst revision about the orchard.", **page)
    )
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    r = ingest(
        client,
        [
            message(
                3600, content="# Orchard\n\nSecond revision about the orchard.", **page
            )
        ],
    )
    assert r.status_code == 200, r.text
    person = ok(client.post("/v1/identity/people", json={"handle": "synthetic-friend"}))
    for identifier in ok(client.get("/v1/identity/unclaimed")):
        ok(
            client.post(
                "/v1/identity/attach",
                json={"identifier_id": identifier["id"], "person_id": person["id"]},
            )
        )
    ok(
        client.post(
            "/v1/entities",
            json={
                "kind": "place",
                "name": "Synthetic place",
                "metadata": {"lat": 1, "lon": 2},
            },
        )
    )
    ok(
        client.post(
            "/v1/links",
            json={
                "type": "about",
                "from": {"channel": "synthetic.chat", "external_id": "fake-0"},
                "to": {"kind": "work", "external_ref": "fake-work"},
                "valid_from": "2020-01-01",
                "valid_from_precision": "day",
                "valid_to": "2021-06-01T00:00:00Z",
                "valid_to_precision": "exact",
            },
        )
    )
    # The feed of the feed_loop recipe (source coverage, when installed).
    if has_route(client, "POST", "/v1/feeds"):
        ok(
            client.post(
                "/v1/feeds", json={"name": "recipe_demo", "start": "beginning"}
            )
        )
    build_people(client, person["id"])


def build_people(client, friend):
    """The person part of the corpus (people recipes): an owner whose
    account is unattached (the owner side comes from direction outgoing), one
    exchange with synthetic-friend, an ask each way that nobody answered."""
    owner = ok(
        client.post(
            "/v1/identity/people",
            json={"handle": "synthetic-owner", "is_owner": True},
        )
    )
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name="synthetic.dm",
                plugin="synthetic",
                fields=[
                    dict(name="direction", type="text", description="Synthetic"),
                    dict(
                        name="conversation_kind", type="text", description="Synthetic"
                    ),
                ],
            ),
        )
    )
    friend_account = dict(system="fake", kind="account", value="fake-account")
    me = dict(system="fake", kind="account", value="synthetic-me")
    turns = (
        ("dm-1", 86400, friend_account, "Could you send the orchard map?", {}),
        (
            "dm-1",
            86520,
            me,
            "Sent the orchard map just now.",
            {"direction": "outgoing"},
        ),
        ("dm-1", 86640, me, "Did the pear trees arrive?", {"direction": "outgoing"}),
        ("dm-2", 89900, me, "Morning from the orchard.", {"direction": "outgoing"}),
        ("dm-2", 90000, friend_account, "Can you bring the ladder on Sunday?", {}),
    )
    items = [
        message(
            at,
            channel="synthetic.dm",
            external_id=f"dm-{n}",
            thread={"external_id": thread},
            content=content,
            author=author,
            fields=dict(fields, conversation_kind="dm"),
        )
        for n, (thread, at, author, content, fields) in enumerate(turns)
    ]
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    # Person routes refresh the exchange index before they answer; SQL
    # callers read api.exchange_index_status().
    if has_route(client, "GET", "/v1/people/{person_id}/brief"):
        ok(client.get(f"/v1/people/{friend}/brief"))
    return owner["id"]


@pytest.fixture()
def corpus(client):
    build_corpus(client)
    return client


@pytest.fixture()
def probe_cleanup(database):
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.recipes WHERE name LIKE 'zz\\_%'")
        c.execute("DELETE FROM tl.catalog_sections WHERE subject LIKE 'recipe:zz\\_%'")


def write_recipe(
    directory, name=PROBE, version=1, status="tested", body="Probe.", **meta
):
    front = dict(
        name=name,
        version=version,
        schema_version="0045",
        status=status,
        summary="A synthetic probe recipe.",
        needs=[],
        statements=["SELECT api.describe() AS index"],
        expect={"min_rows": 1},
    )
    front.update(meta)
    path = Path(directory) / f"{name}.md"
    path.write_text(
        "---\n" + json.dumps(front, indent=2) + "\n---\n\n# Probe\n\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def owner_scope():
    with connect() as c:
        token = c.execute(
            "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
    return Scope.from_token(dict(token, asker=None))


# --- INT-9 -------------------------------------------------------------------


def test_int_9_every_tested_recipe_executes_and_meets_expectation(corpus):
    files = recipes.load_files()
    tested = {name: r for name, r in files.items() if r["status"] == "tested"}
    assert len(tested) >= len(CORE) + len(QRY_12) + len(BEHAVIOUR_RECIPES)
    reports = {name: recipes.run(corpus, recipe) for name, recipe in tested.items()}
    failed = {n: r for n, r in reports.items() if r["status"] == "failed"}
    assert not failed, json.dumps(failed, indent=1, default=str)
    skipped = {n: r["missing"] for n, r in reports.items() if r["status"] == "skipped"}
    # Only recipes of optional features not installed may skip, each with
    # the missing need named.
    allowed = {"coverage_first", "feed_loop"} | set(OPTIONAL_FEATURE_RECIPES)
    assert set(skipped) <= allowed, skipped
    for name, missing in skipped.items():
        assert missing and all(recipes.NEED.fullmatch(m) for m in missing), name
    for name in CORE + QRY_12:
        assert reports[name]["status"] == "passed", (name, reports[name])
    # Handles flow from a semantic call to the next statement.
    assert reports["recall_then_context"]["steps"][0]["handles"] == ["handle", "r"]
    # The same recipes pass through the reader path under a Scope.
    scope = owner_scope()
    for name in ("catalogue_first", "refine_saved_set", "as_of_page_lookup"):
        assert recipes.run(scope, name)["status"] == "passed", name
    # A recipe whose expectation is not met fails; a need that is absent skips.
    broken = dict(tested["catalogue_first"], expect={"min_rows": 5})
    assert recipes.run(corpus, broken)["status"] == "failed"
    absent = dict(tested["catalogue_first"], needs=["function:no_such_function"])
    report = recipes.run(corpus, absent)
    assert report == dict(
        report, status="skipped", missing=["function:no_such_function"]
    )
    bad_sql = dict(tested["catalogue_first"], statements=["SELECT * FROM tl.recipes"])
    assert recipes.run(corpus, bad_sql)["status"] == "failed"


def test_int_9_versioning(database, tmp_path, probe_cleanup):
    write_recipe(tmp_path)
    with connect() as c:
        result = recipes.sync(c, tmp_path)
        assert (PROBE, 1) in result["inserted"] and PROBE in result["listed"]
        # Syncing the same file again stores nothing new.
        assert recipes.sync(c, tmp_path)["inserted"] == []
    # A changed file without a version bump is refused.
    write_recipe(tmp_path, body="Changed text, same version.")
    with (
        connect() as c,
        pytest.raises(recipes.RecipeError, match="without a version bump"),
    ):
        recipes.sync(c, tmp_path)
    # A version bump stores a second row; the first is kept unchanged.
    write_recipe(tmp_path, version=2, body="Changed text, version two.")
    with connect() as c:
        assert recipes.sync(c, tmp_path)["inserted"] == [(PROBE, 2)]
        versions = c.execute(
            "SELECT version,body FROM tl.recipes WHERE name=%s ORDER BY version",
            (PROBE,),
        ).fetchall()
        listed = c.execute(
            "SELECT version FROM api.recipes WHERE name=%s", (PROBE,)
        ).fetchall()
        section = c.execute(
            "SELECT version,body FROM tl.catalog_sections WHERE subject=%s",
            ("recipe:" + PROBE,),
        ).fetchone()
    assert [v["version"] for v in versions] == [1, 2]
    assert "Probe." in versions[0]["body"] and "version two" in versions[1]["body"]
    assert listed == [{"version": 2}]
    assert section["version"] == 2 and section["body"]["version"] == 2
    # An older version than the stored one is refused too.
    write_recipe(tmp_path, version=1, body="Probe.")
    with connect() as c, pytest.raises(recipes.RecipeError, match="older"):
        recipes.sync(c, tmp_path)
    # The shipped files are all stored under their versions (db._migrate ran
    # sync), and a repeated sync of the package is a no-op.
    with connect() as c:
        assert recipes.sync(c)["inserted"] == []
        stored = {
            (r["name"], r["version"]): r["content_hash"]
            for r in c.execute("SELECT name,version,content_hash FROM tl.recipes")
        }
    for name, recipe in recipes.load_files().items():
        assert stored[(name, recipe["version"])] == recipe["content_hash"], name
    # Malformed files stop loading instead of being guessed.
    for bad in (
        "no front matter",
        "---\nnot json\n---\nbody",
        '---\n{"name": "x"}\n---\nbody',
    ):
        with pytest.raises(recipes.RecipeError):
            recipes.parse(bad)
    other = tmp_path / "renamed"
    other.mkdir()
    write_recipe(other, name="zz_other").rename(other / "zz_renamed.md")
    with pytest.raises(recipes.RecipeError, match="file name"):
        recipes.load_files(other)


def test_int_9_proposed_not_listed(client, tmp_path, probe_cleanup):
    write_recipe(tmp_path, status="proposed")
    with connect() as c:
        recipes.sync(c, tmp_path)
        assert c.execute(
            "SELECT status FROM tl.recipes WHERE name=%s", (PROBE,)
        ).fetchone() == {"status": "proposed"}
    assert PROBE not in {r["name"] for r in ok(client.get("/v1/recipes"))}
    assert client.get(f"/v1/recipes/{PROBE}").status_code == 404
    described = ok(client.get("/v1/describe", params={"subject": "recipes"}))
    assert PROBE not in {r["name"] for r in described["recipes"]}
    assert ok(client.get("/v1/describe", params={"subject": "recipe:" + PROBE})) is None
    # A tested file replaces the proposal; only then is it listed.
    write_recipe(tmp_path, version=2, status="tested")
    with connect() as c:
        recipes.sync(c, tmp_path)
    assert PROBE in {r["name"] for r in ok(client.get("/v1/recipes"))}
    # A later proposal does not hide (or replace) the tested version.
    write_recipe(tmp_path, version=3, status="proposed", body="Draft.")
    with connect() as c:
        recipes.sync(c, tmp_path)
    assert ok(client.get(f"/v1/recipes/{PROBE}"))["version"] == 2
    # Retiring removes the name.
    write_recipe(tmp_path, version=4, status="retired")
    with connect() as c:
        recipes.sync(c, tmp_path)
    assert client.get(f"/v1/recipes/{PROBE}").status_code == 404
    assert ok(client.get("/v1/describe", params={"subject": "recipe:" + PROBE})) is None
    # No shipped proposal is listed either.
    shipped = recipes.load_files()
    listed = {r["name"] for r in ok(client.get("/v1/recipes"))}
    for name, recipe in shipped.items():
        assert (name in listed) == (recipe["status"] == "tested"), name


def test_int_9_discoverable_through_describe_and_sql(corpus, monkeypatch):
    client = corpus
    tested = sorted(
        n for n, r in recipes.load_files().items() if r["status"] == "tested"
    )
    index = ok(client.get("/v1/describe"))
    assert "recipes" in {s["subject"] for s in index["subjects"]}
    described = ok(client.get("/v1/describe", params={"subject": "recipes"}))
    assert [r["name"] for r in described["recipes"]] == tested
    one = ok(client.get("/v1/describe", params={"subject": "recipe:catalogue_first"}))
    assert one["statements"][0] == "SELECT api.describe() AS index"
    assert "Troubleshooting" in one["body"] and one["status"] == "tested"
    rows = ok(
        client.post(
            "/v1/sql",
            json={
                "statement": "SELECT name, version, statements FROM api.recipes ORDER BY name"
            },
        )
    )["rows"]
    assert [r["name"] for r in rows] == tested
    assert [r["name"] for r in ok(client.get("/v1/recipes"))] == tested
    assert "body" in ok(client.get("/v1/recipes/catalogue_first"))
    # A restricted asker reads the same recipes: they hold no personal data.
    access.register_asker("recipe-guest")
    try:
        access.grant("recipe-guest", channel="synthetic.chat")
        agent = create_token("agent", kind="agent", askers=["recipe-guest"])["token"]
        headers = {"Authorization": "Bearer " + agent}
        assert [
            r["name"] for r in ok(client.get("/v1/recipes", headers=headers))
        ] == tested
        guest = ok(
            client.get("/v1/describe", params={"subject": "recipes"}, headers=headers)
        )
        assert guest == described
    finally:
        access.remove_asker("recipe-guest")
    # CLI: lib recipe list | show | run over the same routes.
    import throughline.cli as cli

    def request(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code < 400, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", request)
    runner = CliRunner()
    listed = runner.invoke(cli.app, ["lib", "recipe", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [r["name"] for r in json.loads(listed.output)] == tested
    shown = runner.invoke(cli.app, ["lib", "recipe", "show", "context_around_hit"])
    assert shown.exit_code == 0 and "api.context(" in shown.output, shown.output
    ran = runner.invoke(cli.app, ["lib", "recipe", "run", "refine_saved_set", "--json"])
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.output)["status"] == "passed"


def test_recipe_schema_version_known(database):
    migrations = sorted(p.name for p in (SRC / "migrations").glob("*.sql"))
    numbers = {name[:4] for name in migrations}
    with connect() as c:
        applied = {
            r["version"][:4]
            for r in c.execute("SELECT version FROM tl.schema_migrations")
        }
        stored = c.execute(
            "SELECT name,schema_version FROM tl.recipes WHERE name NOT LIKE 'zz\\_%'"
        ).fetchall()
    assert "0045" in numbers
    for name, recipe in recipes.load_files().items():
        assert recipe["schema_version"] in numbers, name
        assert recipe["schema_version"] in applied, name
        # Written against 0045 or later: the recipe table exists from there on.
        assert recipe["schema_version"] >= "0045", name
    assert all(r["schema_version"] in numbers for r in stored)


def test_behaviour_recipes_present(database):
    files = recipes.load_files()
    for name, needs in BEHAVIOUR_RECIPES.items():
        assert name in files, name
        recipe = files[name]
        assert recipe["status"] == "tested", name
        assert set(needs) <= set(recipe["needs"]), name
    text = {name: files[name]["body"] for name in BEHAVIOUR_RECIPES}
    assert "not found in covered sources" in text["coverage_first"].lower()
    assert "not found in coverage" in text["coverage_first"].lower()
    assert "gap is not absence" in text["coverage_first"]
    assert re.search(r"mark after work", text["feed_loop"], re.I)
    assert "late backfill" in text["feed_loop"]
    assert "episode_id" in text["incidents_by_episode"]
    assert "evidence_class" in text["cite_primary_evidence"]
    for phrase in ("include_ignored", "/v1/annotations", "un-ignores", "effective"):
        assert phrase in text["annotate_not_rewrite"], phrase
    assert "/v1/link-types" in text["link_type_registry"]
    assert '"kind": "link_type"' in text["link_type_registry"]
    # The troubleshooting topics of the brief are covered across the recipes.
    everything = " ".join(r["body"] for r in files.values()).lower()
    for topic in (
        "filters only the candidates",
        "aggregate over ranked results is not a count",
        "validity interval",
        "author versus involved",
        "time_basis",
        "known_by is an ingest-time cutoff",
    ):
        assert topic in everything, topic


def test_behaviour_recipes_tested(corpus):
    client = corpus
    missing = [
        need
        for need in ("function:coverage_for", "function:news", "function:pending")
        if recipes.missing_needs(client, {"needs": [need]})
    ]
    if missing or not has_route(client, "POST", "/v1/feeds"):
        pytest.skip(f"optional feature: source coverage not installed ({missing})")
    for name in ("coverage_first", "feed_loop"):
        report = recipes.run(client, name)
        assert report["status"] == "passed", report


def test_recipe_handles_are_only_result_set_handles():
    handle = "rs_" + "0" * 32
    statement = "SELECT * FROM api.result_set('{{handle}}')"
    assert recipes.render(statement, {"handle": handle}).endswith(f"'{handle}')")
    # Only a result set handle is ever spliced into SQL: a value that came
    # back from a server in any other shape stops the recipe.
    for value in ("x') UNION SELECT * FROM tl.tokens --", "", None, "rs_XYZ"):
        with pytest.raises(recipes.RecipeError):
            recipes.render(statement, {"handle": value})
    with pytest.raises(recipes.RecipeError, match="no handle named"):
        recipes.render(statement, {})
    # A failed render is a failed step, never an exception out of run.
    recipe = dict(
        recipes.parse(
            "---\n"
            + json.dumps(
                dict(
                    name="zz_render",
                    version=1,
                    schema_version="0045",
                    status="tested",
                    summary="Probe.",
                    statements=[statement],
                )
            )
            + "\n---\nbody\n"
        )
    )
    calls = []
    report = recipes.run(recipes._HttpTarget(lambda *a: calls.append(a)), recipe)
    assert report["status"] == "failed" and "no handle named" in report["problems"][0]
    assert calls == []  # the statement never reached the server


# --- people and ops recipes ---------------------------------------------------


def test_people_and_ops_recipes_execute(corpus):
    client = corpus
    files = recipes.load_files()
    for name, needs in {**PEOPLE_OPS_RECIPES, **OPTIONAL_FEATURE_RECIPES}.items():
        assert name in files, name
        assert files[name]["status"] == "tested", name
        assert set(needs) <= set(files[name]["needs"]), name
        assert files[name]["schema_version"] == "0052", name
    reports = {name: recipes.run(client, name) for name in PEOPLE_OPS_RECIPES}
    failed = {n: r for n, r in reports.items() if r["status"] != "passed"}
    assert not failed, json.dumps(failed, indent=1, default=str)
    # The answers mean what the recipes say on the fixture corpus.
    with connect() as c:
        friend = c.execute(
            "SELECT id FROM tl.people WHERE handle='synthetic-friend'"
        ).fetchone()["id"]
        owner = c.execute("SELECT id FROM tl.people WHERE is_owner").fetchone()["id"]

    def rows(statement):
        return ok(client.post("/v1/sql", json={"statement": statement}))["rows"]

    first = recipes.load_files()["person_brief_first"]["statements"][0]
    brief = rows(first)[0]
    assert brief["person"] == friend and brief["exchange_count"] >= 1
    assert brief["open_loop_count"] is not None
    loops = rows(files["open_loops_both_ways"]["statements"][0])
    assert {r["direction"] for r in loops} == {"owner_owes", "they_owe"}
    between = rows(files["between_versus_author"]["statements"][0])
    assert {r["party_id"] for r in between} == {owner, friend}
    assert any(r["is_me"] for r in between) and not all(r["is_me"] for r in between)
    circle = rows(files["circle_and_ties"]["statements"][0])
    assert circle and circle[0]["other_person_id"] == friend
    ties = rows(files["circle_and_ties"]["statements"][1])
    assert ties and ties[0]["exchanges"] >= 1
    last = rows(files["last_contact_pitfall"]["statements"][0])[0]
    assert last["last_contact_at"] and last["last_exchange_at"]
    contract = rows(files["write_with_receipt"]["statements"][0])[0]["contract"]
    rules = " ".join(contract["rules"])
    assert "consumer_cursor" in rules and "replay" in rules.lower()
    ops = rows(files["verify_backup_alarm"]["statements"][0])[0]["ops"]
    assert "verify-restore" in json.dumps(ops) and "alarm" in ops["commands"]
    # Brief first: the write recipe's body and the skill send a writer
    # through a key and a receipt, never a read-back.
    body = files["write_with_receipt"]["body"]
    for phrase in ("Idempotency-Key", "replayed", "409", "Do not read back"):
        assert phrase in body, phrase
    for name in ("between_versus_author", "last_contact_pitfall"):
        assert "Troubleshooting" in files[name]["body"], name


def test_commitment_and_disclosure_recipes(corpus):
    """Optional features: the spoken-instruction-to-action (commitments) and disclosure
    (disclosure) recipes execute once their features are installed."""
    client = corpus
    missing = {
        name: recipes.missing_needs(client, files)
        for name, files in recipes.load_files().items()
        if name in OPTIONAL_FEATURE_RECIPES
    }
    for name, need in missing.items():
        report = recipes.run(client, name)
        if need:
            assert report["status"] == "skipped" and report["missing"] == need, report
        else:
            assert report["status"] == "passed", report
    absent = {n: m for n, m in missing.items() if m}
    if absent:
        pytest.skip(f"optional feature not installed: {absent}")


def test_recipes_moved_out_of_core_are_retired(database):
    """Recipes that left core are retired explicitly by migration 0075: an
    upgraded database stops listing its stored versions."""
    moved = ("burn_vs_calendar", "subscription_status", "work_cadence", "work_concentration")
    assert not set(moved) & set(recipes.load_files())
    migration = (SRC / "migrations" / "0078_retire_connector_recipes.sql").read_text()
    with connect() as c:
        c.execute("DELETE FROM tl.recipes WHERE name = ANY(%s)", (list(moved),))
        for name in moved:
            c.execute(
                "INSERT INTO tl.recipes(name,version,schema_version,status,summary,body,statements,content_hash)"
                " VALUES (%s,1,'0056','tested','synthetic','body','[\"SELECT 1\"]','h')",
                (name,),
            )
        recipes.sync(c)
        listed = {r["name"] for r in c.execute("SELECT name FROM api.recipes")}
        assert set(moved) <= listed
        c.execute(migration)
        c.execute(migration)  # idempotent: one retired version each
        recipes.sync(c)
        listed = {r["name"] for r in c.execute("SELECT name FROM api.recipes")}
        assert not set(moved) & listed
        sections = {
            r["subject"]
            for r in c.execute("SELECT subject FROM tl.catalog_sections WHERE subject LIKE 'recipe:%%'")
        }
        assert not {"recipe:" + n for n in moved} & sections
        rows = c.execute(
            "SELECT name, max(version) AS v, count(*) AS n FROM tl.recipes WHERE name = ANY(%s) GROUP BY name",
            (list(moved),),
        ).fetchall()
        assert {(r["name"], r["v"], r["n"]) for r in rows} == {(n, 2, 2) for n in moved}
        c.execute("DELETE FROM tl.recipes WHERE name = ANY(%s)", (list(moved),))
