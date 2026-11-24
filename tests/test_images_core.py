"""Images as a native modality, core half (migration 0063, docs/images.md):
schema objects, gating, depicts links, images[] in responses, the visual and
image_lexical legs with fake embedders, and the asset routes with Immich
mocked."""

import base64
import io
import json
import uuid

import httpx
import pytest
from conftest import ingest, message
from fastapi.testclient import TestClient

from throughline import access
from throughline.api.routes import assets as assets_route
from throughline.db import connect, create_token
from throughline.images import store
from throughline.providers import Usage
from throughline.providers import visual as visual_models

PUBLIC, SECRET = "chat.synthetic", "synthetic.hidden"
GUEST = "guest"
CANARY = "zqimgcanary"
PERSON_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_UUID = "22222222-2222-2222-2222-222222222222"


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def message_id(channel, external_id):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id"
            " WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


class FakeVisual:
    """A query embedder of an image space (providers.visual interface)."""

    name = "fake"
    version = "fake-visual-v1"
    local = True
    price_per_million = 0.0

    def __init__(self, model, vector):
        self.model = model
        self.vector = list(vector)
        self.dimensions = len(vector)
        self.calls = 0

    def embed(self, texts, model=None, dimensions=None, input_type="query"):
        self.calls += 1
        return [list(self.vector) for _ in texts], Usage(model=self.model, calls=1)


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


@pytest.fixture()
def world(client):
    for name in (PUBLIC, SECRET):
        ok(client.post("/v1/channels", json=dict(name=name, plugin="synthetic")))
    items = [
        message(0, channel=PUBLIC, external_id="pub-0", content="Lunch at the pier", fields={}),
        message(60, channel=PUBLIC, external_id="pub-1", content="Look at this", fields={}),
        message(120, channel=PUBLIC, external_id="pub-2", content="Plain words only", fields={}),
        message(
            30,
            channel=SECRET,
            external_id="sec-0",
            thread={"external_id": "secret-thread"},
            content=f"Hidden {CANARY} photo",
            fields={},
        ),
    ]
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    ids = {k: message_id(PUBLIC, k) for k in ("pub-0", "pub-1", "pub-2")}
    ids["sec-0"] = message_id(SECRET, "sec-0")
    with connect() as c:
        alex = c.execute(
            "INSERT INTO tl.people(handle,display_name) VALUES ('alex','Alex Kim') RETURNING id"
        ).fetchone()["id"]
        sam = c.execute(
            "INSERT INTO tl.people(handle,display_name) VALUES ('sam','Sam Lee') RETURNING id"
        ).fetchone()["id"]
        a1 = store.upsert_asset(c, sha256="a" * 64, origin="beeper", mime="image/png", width=800, height=600, original_name="IMG_0001.png")
        store.set_asset(
            c,
            a1,
            immich_id=str(uuid.uuid4()),
            kind="photo",
            tags=["Beach", "sunset"],
            ocr_text="WELCOME TO GALVESTON",
            place={"city": "Galveston", "state": "Texas", "country": "US"},
            lat=29.30,
            lon=-94.79,
            taken_at="2020-01-01T00:00:00+00:00",
            status="enriched",
            nsfw=0.01,
            merge_enrich={"pull": {"v": 1}, "nsfw": {"v": 1}},
        )
        a2 = store.upsert_asset(c, sha256="b" * 64, origin="beeper", mime="image/png")
        store.set_asset(c, a2, immich_id=str(uuid.uuid4()), kind="photo", nsfw=0.9, tags=["beach"])
        a3 = store.upsert_asset(c, sha256="c" * 64, origin="beeper", mime="image/png")
        store.set_asset(c, a3, immich_id=str(uuid.uuid4()), kind="screenshot", tags=[CANARY, "beach"], caption=f"{CANARY} caption", nsfw=0.0)
        store.link_message(c, channel=PUBLIC, external_id="pub-0", asset_id=a1)
        store.link_message(c, channel=PUBLIC, external_id="pub-1", asset_id=a2)
        store.link_message(c, channel=SECRET, external_id="sec-0", asset_id=a3, source_ref={"url": CANARY})
        store.replace_faces(
            c,
            a1,
            [
                dict(immich_face_id=str(uuid.uuid4()), immich_person_id=PERSON_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9}, score=0.9, name="Alex Kim"),
                dict(immich_face_id=str(uuid.uuid4()), immich_person_id=None, bbox={"x1": 2, "y1": 2, "x2": 8, "y2": 8}, score=0.5),
            ],
        )
        store.replace_faces(
            c,
            a3,
            [dict(immich_face_id=str(uuid.uuid4()), immich_person_id=PERSON_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9}, score=0.9)],
        )
        for asset_id, vector in ((a1, [1, 0, 0]), (a2, [0.95, 0.05, 0]), (a3, [1, 0, 0.05])):
            store.put_vector(c, asset_id, visual_models.GEMINI, vector)
            store.put_vector(c, asset_id, visual_models.SIGLIP, vector)
        # The owner confirms the face cluster as Alex (it was suggested by name).
        identifier = c.execute(
            "SELECT id,person_id,status FROM tl.identifiers WHERE system='immich' AND kind='face' AND value=%s",
            (PERSON_UUID,),
        ).fetchone()
        assert identifier["person_id"] == alex and identifier["status"] == "suspected"
        assert not c.execute("SELECT 1 FROM tl.links WHERE type='depicts'").fetchone()
        # Confirming writes the depicts links (trigger on tl.identifiers).
        c.execute("UPDATE tl.identifiers SET status='confirmed' WHERE id=%s", (identifier["id"],))
        assert c.execute("SELECT count(*) AS n FROM tl.links WHERE type='depicts'").fetchone()["n"] == 2
        counts = store.refresh_depicts(c)
        assert counts == dict(added=0, removed=0, updated=0), counts
    access.register_asker(GUEST)
    access.grant(GUEST, channel=PUBLIC)
    agent = create_token("guest agent", kind="agent", askers=[GUEST])
    guest = TestClient(client.app)
    guest.__enter__()
    guest.headers["Authorization"] = "Bearer " + agent["token"]
    yield dict(client=client, guest=guest, ids=ids, alex=alex, sam=sam, a1=a1, a2=a2, a3=a3)
    guest.__exit__(None, None, None)


def sql(caller, statement):
    return ok(caller.post("/v1/sql", json={"statement": statement}))["rows"]


def leaks(value):
    return CANARY in json.dumps(value, default=str).lower()


# --- schema ------------------------------------------------------------------------


def test_migration_objects(client):
    with connect() as c:
        for table in ("assets", "message_assets", "asset_faces", "asset_vectors"):
            assert c.execute("SELECT to_regclass(%s) AS r", (f"tl.{table}",)).fetchone()["r"]
        gates = {r["view_name"]: r["predicate"] for r in c.execute("SELECT * FROM tl.gates")}
        assert gates["asset_faces"] == "false"
        assert "message_visible" in gates["assets"] and "message_visible" in gates["message_assets"]
        # Seeded registries come back after the per-test truncation.
        assert c.execute("SELECT inverse FROM tl.link_types WHERE type='depicts'").fetchone()["inverse"] == "depicted_in"
        assert c.execute(
            "SELECT normalizer FROM tl.identifier_schemes WHERE system='immich' AND kind='face'"
        ).fetchone()["normalizer"] == "lower"
        assert c.execute("SELECT 1 FROM tl.purge_hooks WHERE name='message_assets'").fetchone()
        names = {r["name"] for r in c.execute("SELECT name FROM tl.api_functions")}
        assert {"assets_of", "visual_records", "image_search_records", "visual_assets", "image_search_assets"} <= names
        indexes = {r["indexname"] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='tl' AND tablename='assets'")}
        assert {"assets_tsv", "assets_tags", "assets_taken_at", "assets_earth", "assets_kind", "assets_origin", "assets_status"} <= indexes


def test_vector_index_per_model_and_size(world):
    with connect() as c:
        names = {
            r["indexname"]
            for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='tl' AND tablename='asset_vectors'")
        }
        for model in (visual_models.GEMINI, visual_models.SIGLIP):
            assert c.execute("SELECT tl.embedding_index_name('asset_vectors',%s,3) AS n", (model,)).fetchone()["n"] in names
        assert c.execute("SELECT tl.ensure_asset_vector_indexes() AS n").fetchone()["n"] == 2


def test_render_text():
    row = dict(
        kind="photo",
        tags=["beach", "ocean", "sunset"],
        place={"city": "Galveston", "state": "Texas", "country": "US"},
        ocr_text="WELCOME  TO\nGALVESTON",
    )
    assert store.render_text(row, ["Alex Kim", "Sam Lee"]) == (
        "[photo · beach, ocean, sunset · Galveston, Texas, US · with Alex Kim, Sam Lee]\n"
        'text: "WELCOME TO GALVESTON"'
    )
    assert store.render_text({}, []) == "[image]"
    long = store.render_text(dict(kind="document", ocr_text="x" * 400))
    assert len(long.split("text: ", 1)[1]) <= 302


def test_store_assets(client):
    with connect() as c:
        first = store.upsert_asset(c, sha256="D" * 64, origin="email")
        again = store.upsert_asset(c, sha256="d" * 64, origin="beeper", mime="image/jpeg", width=10)
        assert first == again
        row = store.get_asset(c, first)
        assert row["origin"] == "email" and row["mime"] == "image/jpeg" and row["width"] == 10
        assert [r["id"] for r in store.assets_needing(c, "upload", 10)] == [first]
        row = store.set_asset(c, first, tags=["Sunset Beach", "meme", "meme"], nsfw=0.7, merge_enrich={"nsfw": {"v": 1}})
        assert row["tags"] == ["sunset-beach", "meme", "nsfw"] and row["enrich"] == {"nsfw": {"v": 1}}
        store.set_asset(c, first, immich_id=str(uuid.uuid4()), status="uploaded", merge_enrich={"pull": {"v": 1}})
        assert store.assets_needing(c, "upload", 10) == []
        assert [r["id"] for r in store.assets_needing(c, "tags", 10)] == [first]
        assert store.assets_needing(c, "nsfw", 10) == []
        with pytest.raises(ValueError):
            store.set_asset(c, first, kind="selfie")
        with pytest.raises(ValueError):
            store.link_message(c, channel="no.such", external_id="x", asset_id=first)
        tsv = c.execute("SELECT tsv::text AS t FROM tl.assets WHERE id=%s", (first,)).fetchone()["t"]
        assert "sunset" in tsv and "meme" in tsv


# --- gating ---------------------------------------------------------------------------


def test_views_are_gated(world):
    owner, guest = world["client"], world["guest"]
    assert sorted(r["id"] for r in sql(owner, "SELECT id FROM api.assets")) == sorted([world["a1"], world["a2"], world["a3"]])
    assert sorted(r["id"] for r in sql(guest, "SELECT id FROM api.assets")) == sorted([world["a1"], world["a2"]])
    rows = sql(guest, "SELECT * FROM api.message_assets")
    assert {r["message_id"] for r in rows} == {world["ids"]["pub-0"], world["ids"]["pub-1"]}
    assert not leaks(rows)
    assert sql(guest, "SELECT * FROM api.asset_faces") == []
    faces = sql(owner, "SELECT * FROM api.asset_faces WHERE asset_id=%d" % world["a1"])
    assert {f["name"] for f in faces} == {"Alex Kim", None}


def test_assets_of_shape_people_and_nsfw(world):
    owner, guest, ids = world["client"], world["guest"], world["ids"]
    all_ids = "ARRAY[%s]::bigint[]" % ",".join(str(i) for i in ids.values())
    rows = sql(owner, f"SELECT * FROM api.assets_of({all_ids})")
    assert {r["asset_id"] for r in rows} == {world["a1"], world["a3"]}
    a1 = next(r for r in rows if r["asset_id"] == world["a1"])
    assert a1["people"] == [{"person_id": world["alex"], "name": "Alex Kim"}]
    assert a1["ocr"] == "WELCOME TO GALVESTON" and a1["has_media"] is True
    rows = sql(owner, f"SELECT asset_id FROM api.assets_of({all_ids}, true)")
    assert world["a2"] in {r["asset_id"] for r in rows}
    rows = sql(guest, f"SELECT * FROM api.assets_of({all_ids}, true)")
    assert {r["asset_id"] for r in rows} == {world["a1"], world["a2"]}
    assert all(r["people"] == [] for r in rows)
    assert not leaks(rows)


def test_depicts_links_make_photos_involved(world):
    owner, ids = world["client"], world["ids"]
    with connect() as c:
        links = c.execute(
            "SELECT from_id,to_id,method,evidence FROM tl.links WHERE type='depicts' ORDER BY from_id"
        ).fetchall()
    assert {(r["from_id"], r["to_id"]) for r in links} == {(ids["pub-0"], world["alex"]), (ids["sec-0"], world["alex"])}
    assert all(r["method"] == "rule" for r in links)
    scope = {"persons": [world["alex"]], "person_role": "involved"}
    found = ok(owner.post("/v1/recall", json={"query": "pier", "strategy": "keyword", "scope": scope}))
    assert ids["pub-0"] in {r["message_id"] for r in found["records"]}
    timeline = ok(owner.post("/v1/timeline", json={"query": "pier", "semantic": False, "scope": scope}))
    assert timeline["total_matches"] == 1
    # Face recognition is for unrestricted askers: a restricted guest sees
    # no depicts link (not even of a visible message), and "involved" does
    # not reach a photo of the person.
    guest = world["guest"]
    assert sql(owner, "SELECT from_id FROM api.links WHERE type='depicts' ORDER BY from_id") == [
        {"from_id": ids["pub-0"]},
        {"from_id": ids["sec-0"]},
    ]
    assert sql(guest, "SELECT from_id FROM api.links WHERE type='depicts'") == []
    assert sql(guest, "SELECT * FROM api.links_asof(now()) WHERE type='depicts'") == []
    assert ok(guest.post("/v1/timeline", json={"query": "pier", "semantic": False, "scope": scope}))["total_matches"] == 0
    found = ok(guest.post("/v1/recall", json={"query": "pier", "strategy": "keyword", "scope": scope}))
    assert ids["pub-0"] not in {r["message_id"] for r in found["records"]}
    # Unconfirming the cluster removes the links again (trigger).
    with connect() as c:
        c.execute("UPDATE tl.identifiers SET status='suspected' WHERE system='immich' AND value=%s", (PERSON_UUID,))
        assert not c.execute("SELECT 1 FROM tl.links WHERE type='depicts'").fetchone()
        assert store.refresh_depicts(c)["removed"] == 0


def _tsv(asset_id):
    with connect() as c:
        return c.execute("SELECT tsv::text AS t FROM tl.assets WHERE id=%s", (asset_id,)).fetchone()["t"]


def test_identity_changes_refresh_depicts_and_search_text(world):
    owner = world["client"]
    with connect() as c:
        ident = c.execute(
            "SELECT id FROM tl.identifiers WHERE system='immich' AND value=%s", (PERSON_UUID,)
        ).fetchone()["id"]
    assert "'alex'" in _tsv(world["a1"])
    # Ignoring the cluster through the identity route drops the depicts
    # links and the name from the search text at once.
    ok(owner.post(f"/v1/identity/identifiers/{ident}/status", json={"status": "ignored"}))
    with connect() as c:
        assert not c.execute("SELECT 1 FROM tl.links WHERE type='depicts'").fetchone()
    assert "'alex'" not in _tsv(world["a1"]) and "'alex'" not in _tsv(world["a3"])
    assert ok(owner.post("/v1/recall", json={"query": "alex", "legs": ["image_lexical"]}))["records"] == []
    ok(owner.post(f"/v1/identity/identifiers/{ident}/status", json={"status": "confirmed"}))
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM tl.links WHERE type='depicts'").fetchone()["n"] == 2
        # A rename reaches the search text; a merge moves the links.
        c.execute("UPDATE tl.people SET display_name='Alexandra Kim' WHERE id=%s", (world["alex"],))
    assert "'alexandra'" in _tsv(world["a1"])
    from throughline import identity

    with connect() as c:
        identity.merge(c, world["alex"], world["sam"], "owner")
        targets = {r["to_id"] for r in c.execute("SELECT to_id FROM tl.links WHERE type='depicts'")}
    assert targets == {world["sam"]}
    assert "'sam'" in _tsv(world["a1"]) and "'alexandra'" not in _tsv(world["a1"])


def test_face_identifiers_start_suspected(world):
    with connect() as c:
        asset = store.upsert_asset(c, sha256="e" * 64, origin="beeper")
        store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=asset)
        unknown = "33333333-3333-3333-3333-333333333333"
        store.replace_faces(c, asset, [dict(immich_face_id=str(uuid.uuid4()), immich_person_id=unknown, bbox={"x1": 1}, score=1)])
        row = c.execute("SELECT status,person_id FROM tl.identifiers WHERE value=%s", (unknown,)).fetchone()
        assert row == {"status": "suspected", "person_id": None}
        # Attaching a person alone names nobody: only a confirmation does.
        c.execute("UPDATE tl.identifiers SET person_id=%s WHERE value=%s", (world["sam"], unknown))
        assert not c.execute(
            "SELECT 1 FROM tl.links WHERE type='depicts' AND to_id=%s", (world["sam"],)
        ).fetchone()
        assert store.people_names(c, asset) == []


# --- images[] in responses ----------------------------------------------------------------


def test_images_in_find_timeline_orient_recall(world):
    owner, guest, ids = world["client"], world["guest"], world["ids"]
    with visual_models.override("gemini", None), visual_models.override("siglip", None):
        found = ok(owner.post("/v1/find", json={"query": "pier", "window": 3}))
        lines = {m["message_id"]: m for e in found["episodes"] for m in e.get("messages", [])}
        image = lines[ids["pub-0"]]["images"][0]
        assert set(image) == {"asset_id", "kind", "tags", "people", "place", "location_source", "taken_at", "ocr", "caption", "width", "height", "nsfw", "thumb_url", "content_url"}
        assert image["nsfw"] is False and image["location_source"] == "exif"
        assert image["asset_id"] == world["a1"] and image["tags"] == ["beach", "sunset"]
        assert image["people"] == [{"person_id": world["alex"], "name": "Alex Kim"}]
        assert image["thumb_url"] == f"/v1/assets/{world['a1']}/thumb"
        # The nsfw image of pub-1 is left out (no key), unless asked for.
        assert "images" not in lines[ids["pub-1"]] and "images" not in lines[ids["pub-2"]]
        found = ok(owner.post("/v1/find", json={"query": "pier", "window": 3, "include_nsfw": True}))
        lines = {m["message_id"]: m for e in found["episodes"] for m in e.get("messages", [])}
        flagged = lines[ids["pub-1"]]["images"][0]
        assert flagged["asset_id"] == world["a2"] and flagged["nsfw"] is True
        # Not blurred (include_nsfw): its URLs carry include_nsfw so they answer.
        assert flagged["thumb_url"] == f"/v1/assets/{world['a2']}/thumb?include_nsfw=true"

        timeline = ok(owner.post("/v1/timeline", json={"query": "pier", "semantic": False}))
        assert timeline["episodes"][0]["first"]["images"][0]["asset_id"] == world["a1"]

        with connect() as c:
            store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=world["a1"])
        orient = ok(owner.post("/v1/orient", json={"as_of": "2020-01-02", "days": 5}))
        latest = orient["recent_threads"][0]
        assert latest["message_id"] == ids["pub-2"], orient["recent_threads"]
        assert latest["images"][0]["asset_id"] == world["a1"]

        recall = ok(owner.post("/v1/recall", json={"query": "pier", "strategy": "keyword"}))
        records = {r["message_id"]: r for r in recall["records"]}
        assert records[ids["pub-0"]]["images"][0]["asset_id"] == world["a1"]
        assert all(list(r)[-1] == "images" for r in recall["records"])

        # A restricted asker: images of visible messages, people unnamed.
        found = ok(guest.post("/v1/find", json={"query": "pier", "window": 3}))
        image = next(m for e in found["episodes"] for m in e.get("messages", []) if m["message_id"] == ids["pub-0"])["images"][0]
        assert image["people"] == []
        assert not leaks(found)


# --- legs ----------------------------------------------------------------------------------


def _leg(execution, name):
    return next((s for s in execution["stages"] if s.get("name") == name), None)


def test_visual_leg_gemini_then_siglip_fallback(world):
    owner, guest, ids = world["client"], world["guest"], world["ids"]
    gem = FakeVisual(visual_models.GEMINI, [1, 0, 0])
    with visual_models.override("gemini", gem):
        found = ok(owner.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"]}))
        by_id = {r["message_id"]: r for r in found["records"]}
        assert ids["pub-0"] in by_id and ids["sec-0"] in by_id and ids["pub-1"] not in by_id
        detail = next(d for d in by_id[ids["pub-0"]]["discovery"] if d["leg"] == "visual")["detail"]
        assert detail == {"model": visual_models.GEMINI, "asset_id": world["a1"]}
        found = ok(owner.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"], "include_nsfw": True}))
        assert ids["pub-1"] in {r["message_id"] for r in found["records"]}
        found = ok(guest.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"]}))
        assert {r["message_id"] for r in found["records"]} == {ids["pub-0"]}
        assert not leaks(found)
        # Cached: the second identical query makes no call.
        before = gem.calls
        ok(owner.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"]}))
        assert gem.calls == before
    sig = FakeVisual(visual_models.SIGLIP, [1, 0, 0])
    with visual_models.override("gemini", None), visual_models.override("siglip", sig):
        found = ok(owner.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"]}))
        rec = next(r for r in found["records"] if r["message_id"] == ids["pub-0"])
        assert next(d for d in rec["discovery"] if d["leg"] == "visual")["detail"]["model"] == visual_models.SIGLIP
        assert any("fell back" in w for w in found["execution"]["warnings"])
    with visual_models.override("gemini", None), visual_models.override("siglip", None):
        found = ok(owner.post("/v1/recall", json={"query": "sunset waves", "legs": ["visual"]}))
        assert found["records"] == []
        stage = _leg(found["execution"], "visual")
        assert stage["status"] == "skipped" and stage["reason"] == "visual_embeddings_unavailable"
        # Under as_of the image legs are unavailable, not failed.
        found = ok(owner.post("/v1/recall", json={"query": "sunset", "legs": ["visual", "image_lexical"], "scope": {"as_of": "2021-01-01"}}))
        assert _leg(found["execution"], "visual")["reason"] == "images_current_only"


def test_auto_strategy_runs_image_legs(world):
    owner, ids = world["client"], world["ids"]
    with visual_models.override("gemini", FakeVisual(visual_models.GEMINI, [1, 0, 0])):
        found = ok(owner.post("/v1/recall", json={"query": "galveston beach"}))
    legs = {s["name"]: s for s in found["execution"]["stages"] if s.get("kind") == "leg"}
    assert legs["visual"]["status"] == "ok" and legs["image_lexical"]["status"] == "ok"
    assert ids["pub-0"] in {r["message_id"] for r in found["records"]}


def test_image_lexical_leg_hides_names_from_restricted(world):
    owner, guest, ids = world["client"], world["guest"], world["ids"]
    found = ok(owner.post("/v1/recall", json={"query": "galveston", "legs": ["image_lexical"]}))
    assert [r["message_id"] for r in found["records"]] == [ids["pub-0"]]
    found = ok(owner.post("/v1/recall", json={"query": "alex", "legs": ["image_lexical"]}))
    assert {r["message_id"] for r in found["records"]} == {ids["pub-0"], ids["sec-0"]}
    found = ok(guest.post("/v1/recall", json={"query": "alex", "legs": ["image_lexical"]}))
    assert found["records"] == []
    found = ok(guest.post("/v1/recall", json={"query": "galveston", "legs": ["image_lexical"]}))
    assert [r["message_id"] for r in found["records"]] == [ids["pub-0"]]
    found = ok(guest.post("/v1/recall", json={"query": CANARY, "legs": ["image_lexical"]}))
    assert found["records"] == []


def test_timeline_semantic_accepts_visual_hits(world):
    owner, ids = world["client"], world["ids"]
    with visual_models.override("gemini", FakeVisual(visual_models.GEMINI, [1, 0, 0])):
        timeline = ok(owner.post("/v1/timeline", json={"query": "zzunmatchedword", "scope": {"channels": [PUBLIC]}}))
    assert timeline["semantic_only_episodes"] == 1
    assert timeline["episodes"][0]["first"]["message_id"] == ids["pub-0"]


# --- routes ------------------------------------------------------------------------------------


def _png(size=(800, 600)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture()
def immich(monkeypatch):
    calls = []
    body = _png()

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    monkeypatch.setenv("IMMICH_URL", "http://immich.test")
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")
    monkeypatch.setattr(assets_route, "transport", httpx.MockTransport(handler))
    return calls


def test_asset_routes(world, immich):
    owner, guest = world["client"], world["guest"]
    a1, a3 = world["a1"], world["a3"]
    meta = ok(owner.get(f"/v1/assets/{a1}"))
    assert meta["people"] == [{"person_id": world["alex"], "name": "Alex Kim"}]
    assert meta["messages"][0]["message_id"] == world["ids"]["pub-0"]
    assert ok(guest.get(f"/v1/assets/{a1}"))["people"] == []
    # Hidden and missing answer alike, and nothing is fetched for them.
    for path in ("", "/thumb", "/content"):
        hidden, missing = guest.get(f"/v1/assets/{a3}{path}"), guest.get(f"/v1/assets/987654321{path}")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() and not leaks(hidden.json())
    assert immich == []
    thumb = owner.get(f"/v1/assets/{a1}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert immich[-1].url.path.endswith("/thumbnail") and immich[-1].url.params["size"] == "thumbnail"
    content = owner.get(f"/v1/assets/{a1}/content", params={"max_px": 64})
    assert content.status_code == 200
    from PIL import Image

    assert max(Image.open(io.BytesIO(content.content)).size) <= 64
    inline = ok(owner.post("/v1/assets/inline", json={"asset_ids": [a1, a3, world["a2"]], "max_px": 128}))
    assert [i["asset_id"] for i in inline["images"]] == [a1, a3]
    assert inline["images"][0]["data_uri"].startswith("data:image/")
    base64.b64decode(inline["images"][0]["data_uri"].split(",", 1)[1])
    assert inline["skipped"] == [{"asset_id": world["a2"], "reason": "nsfw"}]
    inline = ok(guest.post("/v1/assets/inline", json={"asset_ids": [a3]}))
    assert inline["images"] == [] and inline["skipped"] == [{"asset_id": a3, "reason": "not_found"}]
    assert owner.post("/v1/assets/inline", json={"asset_ids": list(range(1, 10))}).status_code == 422


def test_unsafe_assets_need_include_nsfw_on_direct_routes(world, immich):
    owner, guest, a2 = world["client"], world["guest"], world["a2"]
    with connect() as c:
        # Not scored yet (an image): unsafe until NudeNet answers.
        unscored = store.upsert_asset(c, sha256="f" * 64, origin="beeper", mime="image/png")
        store.set_asset(c, unscored, immich_id=str(uuid.uuid4()), kind="photo", merge_enrich={"pull": {"v": 1}})
        store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=unscored)
    for caller in (owner, guest):
        for asset_id in (a2, unscored):
            for path in ("", "/thumb", "/content"):
                assert caller.get(f"/v1/assets/{asset_id}{path}").status_code == 404, (asset_id, path)
                assert caller.get(f"/v1/assets/{asset_id}{path}", params={"include_nsfw": "true"}).status_code == 200
    meta = ok(owner.get(f"/v1/assets/{a2}", params={"include_nsfw": True}))
    assert meta["nsfw_score"] == 0.9 and meta["nsfw"] is True
    inline = ok(owner.post("/v1/assets/inline", json={"asset_ids": [unscored]}))
    assert inline["skipped"] == [{"asset_id": unscored, "reason": "nsfw"}]
    found = ok(owner.post("/v1/assets/search", json={"channels": [PUBLIC]}))
    assert {i["asset_id"] for i in found["images"]} == {world["a1"]}
    all_ids = "ARRAY[%s]::bigint[]" % world["ids"]["pub-2"]
    assert sql(owner, f"SELECT asset_id FROM api.assets_of({all_ids})") == []
    # api.assets lists every visible asset (owner tooling) with its safe flag.
    flags = {r["id"]: r["safe"] for r in sql(owner, "SELECT id,safe FROM api.assets")}
    assert flags[a2] is False and flags[unscored] is False and flags[world["a1"]] is True
    with connect() as c:
        # A failed or given-up score stays unsafe; a scored or skipped video is safe.
        store.set_asset(c, unscored, merge_enrich={"nsfw": {"v": 1, "error": "x", "gave_up": True}})
        assert store.asset_safe(store.get_asset(c, unscored)) is False
        store.set_asset(c, unscored, kind="video", merge_enrich={"nsfw": {"v": 1, "skipped": "video"}})
        assert store.asset_safe(store.get_asset(c, unscored)) is True
        cases = c.execute(
            "SELECT tl.asset_safe(NULL,'photo','{}') AS unscored, tl.asset_safe(NULL,'photo','{\"nsfw\":{\"error\":\"x\"}}') AS failed,"
            " tl.asset_safe(NULL,'video','{}') AS video, tl.asset_safe(0.2,'photo','{}') AS low, tl.asset_safe(0.6,'photo','{}') AS high,"
            " tl.asset_safe(NULL,NULL,'{\"nsfw\":{\"v\":1}}') AS done"
        ).fetchone()
    assert cases == dict(unscored=False, failed=False, video=True, low=True, high=False, done=True)
    assert owner.get(f"/v1/assets/{unscored}").status_code == 200


def test_image_search_records_scopes_before_the_cap(world):
    owner, ids = world["client"], world["ids"]
    with connect() as c:
        # 5000 better hits that no message shows, and one visible weaker hit.
        c.execute(
            "INSERT INTO tl.assets(sha256,origin,tags) SELECT md5(i::text)||md5('x'||i::text),'test',ARRAY['zzcap']"
            " FROM generate_series(1,5000) i"
        )
        store.set_asset(c, world["a1"], original_name="zzcap.png")
    rows = sql(owner, "SELECT message_id FROM api.image_search_records('zzcap',5)")
    assert [r["message_id"] for r in rows] == [ids["pub-0"]]


def test_asset_routes_without_immich_key(world, monkeypatch):
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    r = world["client"].get(f"/v1/assets/{world['a1']}/thumb")
    assert r.status_code == 503


def test_assets_search(world):
    owner, guest = world["client"], world["guest"]
    a1, a2, a3 = world["a1"], world["a2"], world["a3"]
    with visual_models.override("gemini", None), visual_models.override("siglip", FakeVisual(visual_models.SIGLIP, [1, 0, 0])):
        found = ok(owner.post("/v1/assets/search", json={"query": "galveston"}))
        ids = [i["asset_id"] for i in found["images"]]
        assert ids[0] == a1 and a2 not in ids and a3 in ids
        assert "gemini" in found["skipped_legs"] and found["searched"]["lexical"] == 1
        found = ok(owner.post("/v1/assets/search", json={"query": "galveston", "kind": "screenshot"}))
        assert [i["asset_id"] for i in found["images"]] == [a3]
        found = ok(owner.post("/v1/assets/search", json={"tags": ["beach"], "include_nsfw": True}))
        assert {i["asset_id"] for i in found["images"]} == {a1, a2, a3}
        found = ok(owner.post("/v1/assets/search", json={"persons": [world["alex"]]}))
        assert {i["asset_id"] for i in found["images"]} == {a1, a3}
        found = ok(owner.post("/v1/assets/search", json={"place": "galveston", "channels": [PUBLIC]}))
        assert [i["asset_id"] for i in found["images"]] == [a1]
        found = ok(guest.post("/v1/assets/search", json={"query": "beach"}))
        assert {i["asset_id"] for i in found["images"]} == {a1}
        assert not leaks(found)
        assert ok(guest.post("/v1/assets/search", json={"persons": [world["alex"]]}))["images"] == []


def test_people_photographed_with_and_nearby(world):
    owner, guest = world["client"], world["guest"]
    with connect() as c:
        store.replace_faces(
            c,
            world["a1"],
            [
                dict(immich_face_id=str(uuid.uuid4()), immich_person_id=PERSON_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9}),
                dict(immich_face_id=str(uuid.uuid4()), immich_person_id=OTHER_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9}, name="Sam Lee"),
            ],
        )
        c.execute("UPDATE tl.identifiers SET status='confirmed' WHERE value=%s", (OTHER_UUID,))
        store.refresh_depicts(c)
        # Sam again, 30 minutes later and 200 m away, without Alex.
        near = store.upsert_asset(c, sha256="e" * 64, origin="immich")
        store.set_asset(c, near, lat=29.3015, lon=-94.79, taken_at="2020-01-01T00:30:00+00:00", nsfw=0.0)
        store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=near)
        store.replace_faces(c, near, [dict(immich_face_id=str(uuid.uuid4()), immich_person_id=OTHER_UUID, bbox={"x1": 0, "y1": 0, "x2": 1, "y2": 1})])
        # A third person only nearby (not in Alex's frame).
        third = c.execute("INSERT INTO tl.people(handle,display_name) VALUES ('kai','Kai') RETURNING id").fetchone()["id"]
        ident = c.execute(
            "INSERT INTO tl.identifiers(system,kind,value,value_norm,person_id) VALUES ('immich','face','33','33',%s) RETURNING id",
            (third,),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO tl.asset_faces(asset_id,identifier_id,bbox) VALUES (%s,%s,'{}')", (near, ident)
        )
    alex = world["alex"]
    found = ok(owner.get(f"/v1/people/{alex}/photographed-with"))
    assert [(p["person_id"], p["assets"]) for p in found["people"]] == [(world["sam"], 1)]
    assert found["people"][0]["example_asset_ids"] == [world["a1"]]
    found = ok(owner.get(f"/v1/people/{alex}/nearby"))
    assert [(p["person_id"], p["name"]) for p in found["people"]] == [(third, "Kai")]
    assert found["people"][0]["example_asset_ids"] == [near]
    assert owner.get("/v1/people/987654321/nearby").status_code == 404
    assert guest.get(f"/v1/people/{alex}/photographed-with").status_code == 403
    assert guest.get(f"/v1/people/{alex}/nearby").status_code == 403


def test_purge_hook_removes_links_and_orphans(world):
    with connect() as c:
        channel = c.execute("SELECT id FROM tl.channels WHERE name=%s", (SECRET,)).fetchone()["id"]
        c.execute("SELECT tl.run_purge_hooks(%s)", (channel,))
        assert not c.execute("SELECT 1 FROM tl.message_assets WHERE channel_id=%s", (channel,)).fetchone()
        assert not c.execute("SELECT 1 FROM tl.assets WHERE id=%s", (world["a3"],)).fetchone()
        assert not c.execute("SELECT 1 FROM tl.asset_faces WHERE asset_id=%s", (world["a3"],)).fetchone()
        assert c.execute("SELECT 1 FROM tl.assets WHERE id=%s", (world["a1"],)).fetchone()


# --- image settings (migration 0065) ------------------------------------------------------------


def put_settings(client, **changes):
    return client.put("/v1/images/settings", json=changes)


def all_ids(world):
    return "ARRAY[%s]::bigint[]" % ",".join(str(i) for i in world["ids"].values())


def test_image_settings_routes(world):
    owner, guest = world["client"], world["guest"]
    got = ok(owner.get("/v1/images/settings"))
    assert got["available"] is True
    assert {k: got[k] for k in ("nsfw_mode", "nsfw_threshold", "unscanned", "restricted_nsfw_mode", "shared_previews",
                                 "location_fallback", "location_window_min", "location_max_accuracy_m")} == dict(
        nsfw_mode="hide", nsfw_threshold=0.6, unscanned="hide", restricted_nsfw_mode="hide", shared_previews="include",
        location_fallback="throughline_and_immich", location_window_min=30, location_max_accuracy_m=500)
    changed = ok(put_settings(owner, nsfw_mode="blur", nsfw_threshold=0.75))
    assert changed["nsfw_mode"] == "blur" and changed["nsfw_threshold"] == 0.75 and changed["unscanned"] == "hide"
    assert ok(owner.get("/v1/images/settings"))["nsfw_mode"] == "blur"
    for bad in ({"nsfw_mode": "loud"}, {"nsfw_threshold": 0}, {"nsfw_threshold": 1.5}, {"bogus": 1},
                {"location_window_min": 0}, {"shared_previews": True}):
        assert put_settings(owner, **bad).status_code == 400, bad
    # Owner tokens only: agents and restricted askers are refused.
    agent = {"Authorization": "Bearer " + create_token("settings agent", kind="agent")["token"]}
    assert owner.get("/v1/images/settings", headers=agent).status_code == 403
    assert owner.put("/v1/images/settings", json={"nsfw_mode": "show"}, headers=agent).status_code == 403
    assert guest.get("/v1/images/settings").status_code == 403
    assert guest.put("/v1/images/settings", json={"nsfw_mode": "show"}).status_code == 403
    # Keyed: a receipt, a replay changes nothing, another body is 409.
    keyed = {"Idempotency-Key": "image-settings-0001"}
    first = ok(owner.put("/v1/images/settings", json={"unscanned": "show"}, headers=keyed))
    assert first["receipt"]["kind"] == "image_settings" and first["receipt"]["replayed"] is False
    again = ok(owner.put("/v1/images/settings", json={"unscanned": "show"}, headers=keyed))
    assert again["receipt"]["replayed"] is True and again["receipt"]["stored_hash"] == first["receipt"]["stored_hash"]
    assert owner.put("/v1/images/settings", json={"unscanned": "hide"}, headers=keyed).status_code == 409
    from throughline.images import user_settings

    with connect() as c:
        with pytest.raises(PermissionError):
            user_settings.write(c, False, {"nsfw_mode": "show"})
        assert user_settings.parse("location_window_min", "45") == 45


def test_nsfw_modes_hide_blur_show(world, immich):
    owner, ids, a2 = world["client"], world["ids"], world["a2"]
    # hide (default): left out, direct routes 404 unless include_nsfw.
    assert world["a2"] not in {r["asset_id"] for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert owner.get(f"/v1/assets/{a2}/thumb").status_code == 404
    # blur: listed and flagged; bytes are blurred unless include_nsfw.
    ok(put_settings(owner, nsfw_mode="blur"))
    rows = {r["asset_id"]: r for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert rows[a2]["nsfw"] is True and rows[a2]["blurred"] is True
    assert rows[world["a1"]]["nsfw"] is False and rows[world["a1"]]["blurred"] is False
    found = ok(owner.post("/v1/find", json={"query": "look", "window": 3}))
    image = next(m for e in found["episodes"] for m in e.get("messages", []) if m["message_id"] == ids["pub-1"])["images"][0]
    assert image["nsfw"] is True and image["thumb_url"] == f"/v1/assets/{a2}/thumb"
    meta = ok(owner.get(f"/v1/assets/{a2}"))
    assert meta["nsfw"] is True and meta["thumb_url"] == f"/v1/assets/{a2}/thumb"
    thumb = owner.get(f"/v1/assets/{a2}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    plain = owner.get(f"/v1/assets/{a2}/thumb", params={"include_nsfw": True})
    assert plain.headers["content-type"] == "image/png"
    content = owner.get(f"/v1/assets/{a2}/content", params={"max_px": 128})
    assert content.status_code == 200 and content.headers["content-type"] == "image/jpeg"
    inline = ok(owner.post("/v1/assets/inline", json={"asset_ids": [a2]}))
    assert inline["images"][0]["blurred"] is True and inline["images"][0]["mime"] == "image/jpeg"
    searched = ok(owner.post("/v1/assets/search", json={"channels": [PUBLIC]}))
    card = next(i for i in searched["images"] if i["asset_id"] == a2)
    assert card["nsfw"] is True
    # show: served as is, the flag and the tag stay.
    ok(put_settings(owner, nsfw_mode="show"))
    rows = {r["asset_id"]: r for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert rows[a2]["nsfw"] is True and rows[a2]["blurred"] is False and "nsfw" in rows[a2]["tags"]
    assert owner.get(f"/v1/assets/{a2}/thumb").headers["content-type"] == "image/png"
    rows = sql(owner, "SELECT message_id FROM api.image_search_records('beach',10)")
    assert ids["pub-1"] in {r["message_id"] for r in rows}


def test_restricted_askers_get_restricted_nsfw_mode(world, immich):
    owner, guest, a2 = world["client"], world["guest"], world["a2"]
    ok(put_settings(owner, nsfw_mode="show", restricted_nsfw_mode="hide"))
    assert a2 in {r["asset_id"] for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert a2 not in {r["asset_id"] for r in sql(guest, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert owner.get(f"/v1/assets/{a2}/thumb").status_code == 200
    assert guest.get(f"/v1/assets/{a2}/thumb").status_code == 404
    found = ok(guest.post("/v1/assets/search", json={"query": "beach"}))
    assert a2 not in {i["asset_id"] for i in found["images"]}
    ok(put_settings(owner, restricted_nsfw_mode="blur"))
    rows = {r["asset_id"]: r for r in sql(guest, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert rows[a2]["blurred"] is True
    assert guest.get(f"/v1/assets/{a2}/thumb").headers["content-type"] == "image/jpeg"
    # The owner sees it plain (show) while the guest gets the blurred one.
    assert owner.get(f"/v1/assets/{a2}/thumb").headers["content-type"] == "image/png"
    assert not leaks(sql(guest, f"SELECT * FROM api.assets_of({all_ids(world)})"))


def test_nsfw_threshold_filters_at_once_and_retags_later(world, tmp_path):
    owner, a2 = world["client"], world["a2"]
    assert {r["id"]: r["safe"] for r in sql(owner, "SELECT id,safe FROM api.assets")}[a2] is False
    ok(put_settings(owner, nsfw_threshold=0.95))
    # Filtering uses the score: a2 (0.9) is safe now, although still tagged.
    assert {r["id"]: r["safe"] for r in sql(owner, "SELECT id,safe FROM api.assets")}[a2] is True
    rows = {r["asset_id"]: r for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert rows[a2]["nsfw"] is False and "nsfw" in rows[a2]["tags"]
    with connect() as c:
        assert c.execute("SELECT tl.asset_safe(0.9::real,'photo','{}')").fetchone()["asset_safe"] is True
        # The tag follows the threshold at classification time.
        x = store.upsert_asset(c, sha256="9" * 64, origin="beeper")
        assert "nsfw" not in store.set_asset(c, x, nsfw=0.9)["tags"]
        assert "nsfw" in store.set_asset(c, x, nsfw=0.97)["tags"]
        store.set_asset(c, x, nsfw=0.5)
    # The tags stage brings tags in line with the threshold (here and in Immich).
    from throughline.images.config import Settings
    from throughline.images.pipeline import Pipeline

    fake = RecordingImmich()
    with connect() as c:
        p = Pipeline(c, Settings.from_env({"IMAGES_STATE_DIR": str(tmp_path)}), immich=fake)
        counts = p.retag_nsfw()
        assert counts == {"nsfw_tag_removed": 2}
        tags = c.execute("SELECT tags FROM tl.assets WHERE id=%s", (a2,)).fetchone()["tags"]
    assert "nsfw" not in tags and fake.untagged and fake.untagged[0][1] == ["nsfw"]


def test_unscanned_setting(world):
    owner = world["client"]
    with connect() as c:
        unscored = store.upsert_asset(c, sha256="d" * 64, origin="beeper", mime="image/png")
        store.set_asset(c, unscored, immich_id=str(uuid.uuid4()), kind="photo", merge_enrich={"pull": {"v": 1}})
        store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=unscored)
    assert unscored not in {r["asset_id"] for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    ok(put_settings(owner, unscanned="show"))
    rows = {r["asset_id"]: r for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}
    assert rows[unscored]["nsfw"] is False
    assert world["a2"] not in rows  # scored nsfw stays hidden
    assert owner.get(f"/v1/assets/{unscored}").status_code == 200
    with connect() as c:
        # A manual nsfw tag on an unscored asset still counts.
        store.set_asset(c, unscored, tags=["nsfw"])
    assert unscored not in {r["asset_id"] for r in sql(owner, f"SELECT * FROM api.assets_of({all_ids(world)})")}


class RecordingImmich:
    """Immich calls the location and preview tests look at."""

    def __init__(self, place=None, description="Synthetic · from Alex Kim"):
        self.updates, self.tagged, self.untagged, self.albums = [], [], [], []
        self.place = place or {}
        self.description = description

    def asset(self, iid):
        return {"exifInfo": dict(self.place, description=self.description)}

    def update(self, iid, **fields):
        self.updates.append((iid, fields))

    def tag(self, ids, values):
        self.tagged.append((list(ids), list(values)))

    def untag(self, ids, values):
        self.untagged.append((list(ids), list(values)))

    def add_to_album(self, name, ids):
        self.albums.append((name, list(ids)))


def _pipeline(c, tmp_path, immich, now=None):
    from throughline.images.config import Settings
    from throughline.images.pipeline import Pipeline

    return Pipeline(c, Settings.from_env({"IMAGES_STATE_DIR": str(tmp_path)}), immich=immich, now=now)


def test_shared_post_previews(world, tmp_path):
    owner, alex = world["client"], world["alex"]
    ok(owner.post("/v1/channels", json=dict(name="chat.instagram", plugin="beeper")))
    items = [
        message(200, channel="chat.instagram", external_id="ig-1", content="https://www.instagram.com/reel/DgA_K5oubuS/\n\nreactions: ❤️ @x", fields={}),
        message(210, channel="chat.instagram", external_id="ig-2", content="Look at my dog", fields={}),
    ]
    r = owner.post("/v1/ingest/messages", json=dict(plugin="beeper", messages=items))
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    face = lambda: [dict(immich_face_id=str(uuid.uuid4()), immich_person_id=PERSON_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9})]  # noqa: E731
    with connect() as c:
        cover = store.upsert_asset(c, sha256="7" * 64, origin="beeper", mime="image/jpeg")
        store.set_asset(c, cover, immich_id=str(uuid.uuid4()), kind="photo", tags=["beach"], nsfw=0.0, merge_enrich={"pull": {"v": 1}, "nsfw": {"v": 1}})
        dog = store.upsert_asset(c, sha256="8" * 64, origin="beeper", mime="image/jpeg")
        store.set_asset(c, dog, immich_id=str(uuid.uuid4()), kind="photo", tags=["beach"], nsfw=0.0, merge_enrich={"pull": {"v": 1}, "nsfw": {"v": 1}})
        # Linked before reclassification, as the live data was.
        store.link_message(c, channel="chat.instagram", external_id="ig-1", asset_id=cover)
        store.link_message(c, channel="chat.instagram", external_id="ig-2", asset_id=dog)
        store.replace_faces(c, cover, face())
        store.replace_faces(c, dog, face())
        ig1, ig2 = message_id("chat.instagram", "ig-1"), message_id("chat.instagram", "ig-2")
        depicts = lambda: {r["from_id"] for r in c.execute("SELECT from_id FROM tl.links WHERE type='depicts' AND to_id=%s", (alex,))}  # noqa: E731
        assert {ig1, ig2} <= depicts()
        fake = RecordingImmich()
        p = _pipeline(c, tmp_path, fake)
        dry = p.reclassify_previews(dry_run=True)
        assert dry["by_channel"]["chat.instagram"] == {"links": 2, "preview_links": 1, "network:instagram": 1, "preview_assets": 1}
        assert fake.tagged == []
        report = p.reclassify_previews()
        assert report["links_changed"] == 1 and report["assets_tagged"] == 1 and report["immich_organized"] == 1
        link = c.execute("SELECT role,source_ref FROM tl.message_assets WHERE asset_id=%s", (cover,)).fetchone()
        assert link["role"] == "preview" and link["source_ref"]["shared_url"] == "https://www.instagram.com/reel/DgA_K5oubuS/"
        assert "shared-post" in store.get_asset(c, cover)["tags"]
        # Faces on a cover name nobody: its depicts link is gone, the dog's stays.
        assert ig1 not in depicts() and ig2 in depicts()
        assert c.execute("SELECT count(*) AS n FROM tl.asset_faces WHERE asset_id=%s", (cover,)).fetchone()["n"] == 1
        assert fake.albums == [("Shared posts", [str(store.get_asset(c, cover)["immich_id"])])]
        assert p.reclassify_previews()["links_changed"] == 0  # idempotent
        # A new preview link writes no depicts link either.
        store.link_message(c, channel="chat.instagram", external_id="ig-1", asset_id=dog, ord=1, role="preview")
        assert ig1 not in depicts()
    # In images[] (shared_previews=include) and out of it with exclude.
    rows = sql(owner, f"SELECT asset_id,role FROM api.assets_of(ARRAY[{ig1},{ig2}]::bigint[])")
    assert {(r["asset_id"], r["role"]) for r in rows} == {(cover, "preview"), (dog, "preview"), (dog, "attachment")}
    assert {r["id"]: r["preview"] for r in sql(owner, f"SELECT id,preview FROM api.assets WHERE id IN ({cover},{dog})")} == {cover: True, dog: False}
    ok(put_settings(owner, shared_previews="exclude"))
    rows = sql(owner, f"SELECT asset_id,role FROM api.assets_of(ARRAY[{ig1},{ig2}]::bigint[])")
    assert {(r["asset_id"], r["role"]) for r in rows} == {(dog, "attachment")}
    found = {r["asset_id"] for r in sql(owner, "SELECT * FROM api.image_search_assets('beach',50)")}
    assert cover not in found and dog in found
    assert ig1 not in {r["message_id"] for r in sql(owner, "SELECT message_id FROM api.image_search_records('beach',50)")}
    searched = ok(owner.post("/v1/assets/search", json={"tags": ["beach"]}))
    assert cover not in {i["asset_id"] for i in searched["images"]}
    # Not in photographed-with: a cover is nobody's photo.
    with connect() as c:
        store.replace_faces(c, cover, face() + [dict(immich_face_id=str(uuid.uuid4()), immich_person_id=OTHER_UUID, bbox={"x1": 1, "y1": 1, "x2": 9, "y2": 9})])
        c.execute("UPDATE tl.identifiers SET status='confirmed', person_id=%s WHERE value=%s", (world["sam"], OTHER_UUID))
        row = c.execute(
            "SELECT i.value, (EXISTS(SELECT 1 FROM tl.asset_faces f WHERE f.identifier_id=i.id) AND NOT EXISTS("
            "SELECT 1 FROM tl.asset_faces f JOIN tl.message_assets ma ON ma.asset_id=f.asset_id AND ma.role<>'preview'"
            " WHERE f.identifier_id=i.id)) AS preview_only FROM tl.identifiers i WHERE i.value=%s", (OTHER_UUID,)
        ).fetchone()
        assert row["preview_only"] is True
    with_ = ok(owner.get(f"/v1/people/{alex}/photographed-with"))
    assert world["sam"] not in {p["person_id"] for p in with_["people"]}


TRACK_AT = "2020-01-01T00:10:00+00:00"


def _track(c, at, lat, lon, accuracy):
    return c.execute(
        "INSERT INTO tl.location_track(at,lat,lon,accuracy_m,source) VALUES (%s,%s,%s,%s,'homeassistant') RETURNING id",
        (at, lat, lon, accuracy),
    ).fetchone()["id"]


def test_locate_from_location_history(world, tmp_path):
    from datetime import datetime, timedelta, timezone

    owner, ids = world["client"], world["ids"]
    clock = [datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)]
    fake = RecordingImmich(place={"city": "Austin", "state": "Texas", "country": "United States"})
    with connect() as c:
        aid = store.upsert_asset(c, sha256="6" * 64, origin="beeper", mime="image/png")
        store.set_asset(c, aid, immich_id=str(uuid.uuid4()), kind="photo", nsfw=0.0, taken_at=TRACK_AT,
                        status="enriched", merge_enrich={"pull": {"v": 1}, "nsfw": {"v": 1}})
        store.link_message(c, channel=PUBLIC, external_id="pub-2", asset_id=aid)
        near = _track(c, "2020-01-01T00:05:00+00:00", 30.2672, -97.7431, 20)
        _track(c, "2020-01-01T00:09:00+00:00", 31.0, -98.0, 900)  # too inaccurate
        _track(c, "2020-01-01T00:50:00+00:00", 32.0, -99.0, 5)  # outside the window
        p = _pipeline(c, tmp_path, fake, now=lambda: clock[0])
        counts = p.locate()
        assert counts["located"] == 1 and counts["pushed_to_immich"] == 1, counts
        row = store.get_asset(c, aid)
        assert (row["lat"], row["lon"], row["loc_method"], row["loc_accuracy_m"]) == (30.2672, -97.7431, "track", 20.0)
        assert row["enrich"]["locate"]["track_id"] == near and row["enrich"]["locate"]["immich"]["state"] == "pushed"
        assert fake.updates[-1][1] == {"latitude": 30.2672, "longitude": -97.7431,
                                       "description": "Synthetic · from Alex Kim · location from history"}
        # EXIF wins: a1 has GPS of its own and is never placed by the track.
        assert store.get_asset(c, world["a1"])["loc_method"] == "exif"
        # Nothing new: not looked at again; Immich's place is read back later.
        assert p.locate() == {"geocode_waiting": 1}
        clock[0] += timedelta(minutes=5)
        assert p.locate() == {"geocoded": 1}
        row = store.get_asset(c, aid)
        assert row["place"] == {"city": "Austin", "state": "Texas", "country": "United States"}
        assert store.render_text(row).startswith("[photo · near Austin, Texas, United States (from location history)")
        # A closer point arrives later: the asset moves.
        closer = _track(c, "2020-01-01T00:09:30+00:00", 30.3, -97.7, 10)
        assert p.locate()["moved"] == 1
        assert store.get_asset(c, aid)["lat"] == 30.3
        # Withdrawn points: back to the next one, then to nothing.
        c.execute("INSERT INTO tl.retired_track(track_id,reason) VALUES (%s,'test')", (closer,))
        assert p.locate()["moved"] == 1 and store.get_asset(c, aid)["lat"] == 30.2672
        c.execute("INSERT INTO tl.retired_track(track_id,reason) VALUES (%s,'test')", (near,))
        assert p.locate()["cleared"] == 1
        row = store.get_asset(c, aid)
        assert row["lat"] is None and row["loc_method"] is None and row["place"] is None
        c.execute("DELETE FROM tl.retired_track")
        # 'throughline' writes nothing to Immich; 'off' does nothing.
        c.execute("UPDATE tl.image_settings SET location_fallback='throughline', location_window_min=31")
        p.refresh_prefs()
        pushed = len(fake.updates)
        assert p.locate()["located"] == 1 and len(fake.updates) == pushed
        c.execute("UPDATE tl.image_settings SET location_fallback='off', location_window_min=20")
        p.refresh_prefs()
        assert p.locate() == {}
    image = sql(owner, f"SELECT location_source FROM api.assets_of(ARRAY[{ids['pub-2']}]::bigint[])")
    assert image == [{"location_source": "track"}]
    found = ok(owner.post("/v1/timeline", json={"query": "plain", "semantic": False}))
    first = found["episodes"][0]["first"]
    assert first["images"][0]["location_source"] == "track"
