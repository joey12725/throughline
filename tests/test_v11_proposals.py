"""Shared proposal queue, concept links, and page_sources retrieval stage
(BRN-12, LIB-6, BEN-11, BEN-12, QRY-20).

Also verifies that claims, commitments, and librarian curate have been cleanly
removed while proposals, annotations, and api.effective continue to work.
"""

import json

import pytest
from conftest import ingest, message

from throughline import access, passages
from throughline.db import connect, create_token
from throughline.query import query_ex
from throughline.retrieval import RecallRequest, recall
from throughline.scope import Scope

CHAT = "synthetic.chat"
SECRET = "synthetic.secret"
PAGE = "synthetic.pages"
OWNER = Scope.local()


def ok(response):
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def headers(kind="agent", plugin=None, askers=None):
    token = create_token("synthetic-" + kind, kind, plugin=plugin, askers=askers)[
        "token"
    ]
    out = {"Authorization": "Bearer " + token}
    if askers:
        out["X-Throughline-Asker"] = askers[0]
    return out


def guest():
    access.register_asker("guest")
    access.grant("guest", channel=CHAT)
    return {"X-Throughline-Asker": "guest"}


def owner(statement, params=()):
    with connect() as c:
        cur = c.execute(statement, params)
        return cur.fetchall() if cur.description else []


def mid(external_id, ch=CHAT):
    return owner(
        "SELECT id FROM tl.messages WHERE channel_id=(SELECT id FROM tl.channels WHERE name=%s) AND external_id=%s AND superseded_by IS NULL",
        (ch, external_id),
    )[0]["id"]


def stage(response, name):
    return [s for s in response.execution["stages"] if s["name"] == name]


def ids_of(response):
    return [r["record_id"] for r in response.records]


@pytest.fixture()
def world(client, channel):
    ok(client.post("/v1/channels", json={"name": SECRET, "plugin": "synthetic"}))
    r = ingest(
        client,
        [
            message(0, external_id="fake-0", content="Synthetic orchard opening"),
            message(60, external_id="fake-1", content="Synthetic orchard detail"),
            message(120, external_id="fake-2", content="Synthetic orchard follow-up"),
            message(
                0,
                channel=SECRET,
                external_id="secret-0",
                thread={"external_id": "secret"},
                content="Synthetic private note",
                fields={},
            ),
        ],
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    m = {
        "fake-0": mid("fake-0"),
        "fake-1": mid("fake-1"),
        "fake-2": mid("fake-2"),
        "secret": mid("secret-0", SECRET),
    }
    return client, m


# --- concept links (BRN-12) ----------------------------------------------------


def test_brn_12_novel_entity_kind_zero_migrations(world):
    client, m = world
    migrations = owner("SELECT count(*) AS n FROM tl.schema_migrations")[0]["n"]
    entity = {
        "kind": "synthetic_flavour",
        "external_ref": "flavour:tart",
        "name": "Tart",
    }
    link = ok(
        client.post(
            "/v1/concepts/link",
            json={"target": {"message_id": m["fake-0"]}, "entity": entity},
        )
    )
    assert link["type"] == "about" and (link["from_kind"], link["from_id"]) == (
        "message",
        m["fake-0"],
    )
    assert (
        link["evidence"] == {"message_ids": [m["fake-0"]]} and link["method"] == "owner"
    )
    page_link = ok(
        client.post(
            "/v1/concepts/link",
            json={
                "target": {"channel": CHAT, "external_id": "fake-1"},
                "entity": entity,
            },
        )
    )
    assert page_link["to_id"] == link["to_id"]
    row = owner("SELECT kind,name FROM tl.entities WHERE id=%s", (link["to_id"],))[0]
    assert row == {"kind": "synthetic_flavour", "name": "Tart"}
    assert owner("SELECT count(*) AS n FROM tl.schema_migrations")[0]["n"] == migrations
    agent_link = ok(
        client.post(
            "/v1/concepts/link",
            json={
                "target": {"message_id": m["fake-1"]},
                "entity": {"id": link["to_id"]},
            },
            headers=headers("agent"),
        )
    )
    assert agent_link["method"] == "model" and agent_link["job"] == "agent"


# --- proposals (LIB-6) ---------------------------------------------------------


def test_proposal_reference_check_uniform_404(world):
    client, m = world
    g = guest()
    agent = headers("agent", askers=["guest"])
    answers = []
    for mid_val in (m["secret"], 999999999):
        for h in (g, agent):
            r = client.post(
                "/v1/proposals",
                json={
                    "kind": "annotation",
                    "payload": {
                        "target": {"message_id": mid_val},
                        "attribute": "ignore",
                        "value": {"ignored": True},
                    },
                },
                headers=h,
            )
            answers.append((r.status_code, r.json()))
    assert {json.dumps(a, sort_keys=True) for a in answers} == {
        json.dumps((404, {"detail": "referenced record not found"}), sort_keys=True)
    }
    # Link ends and evidence ids are checked too.
    r = client.post(
        "/v1/proposals",
        json={
            "kind": "link",
            "payload": {
                "type": "about",
                "from": {"kind": "message", "id": m["secret"]},
                "to": {"kind": "entity", "id": 1},
            },
        },
        headers=agent,
    )
    assert r.status_code == 404
    r = client.post(
        "/v1/proposals",
        json={
            "kind": "correction",
            "payload": {"message_id": m["fake-0"], "note": "check"},
            "evidence": {"message_ids": [m["secret"]]},
        },
        headers=agent,
    )
    assert r.status_code == 404
    r = client.post(
        "/v1/proposals",
        json={
            "kind": "correction",
            "payload": {"message_id": m["fake-0"], "note": "check"},
            "evidence": {"message_ids": [m["fake-1"]]},
        },
        headers=agent,
    )
    assert r.status_code == 200 and r.json()["message_ids"] == [
        m["fake-0"],
        m["fake-1"],
    ]
    # Unrestricted callers may name anything that exists.
    assert (
        client.post(
            "/v1/proposals",
            json={
                "kind": "correction",
                "payload": {"message_id": m["secret"], "note": "owner"},
            },
        ).status_code
        == 200
    )


def test_proposals_listing_scoped_to_token_and_asker(world):
    client, m = world
    access.register_asker("guest")
    access.register_asker("visitor")
    for name in ("guest", "visitor"):
        access.grant(name, channel=CHAT)
    agent = create_token("synthetic-two-askers", "agent", askers=["guest", "visitor"])[
        "token"
    ]
    as_guest_ = {"Authorization": "Bearer " + agent, "X-Throughline-Asker": "guest"}
    as_visitor = {"Authorization": "Bearer " + agent, "X-Throughline-Asker": "visitor"}
    other = headers("agent")
    made = {}
    for name, h in (("guest", as_guest_), ("visitor", as_visitor), ("other", other)):
        r = client.post(
            "/v1/proposals",
            json={
                "kind": "correction",
                "payload": {"message_id": m["fake-0"], "note": f"From {name}"},
                "origin": "agent",
            },
            headers=h,
        )
        made[name] = ok(r)["id"]
    owner_view = {p["id"] for p in ok(client.get("/v1/proposals"))}
    assert set(made.values()) <= owner_view
    assert [p["id"] for p in ok(client.get("/v1/proposals", headers=as_guest_))] == [
        made["guest"]
    ]
    assert [p["id"] for p in ok(client.get("/v1/proposals", headers=as_visitor))] == [
        made["visitor"]
    ]
    assert [p["id"] for p in ok(client.get("/v1/proposals", headers=other))] == [
        made["other"]
    ]
    assert (
        ok(client.get("/v1/proposals", headers={"X-Throughline-Asker": "guest"})) == []
    )


def test_proposals_accept_keeps_originating_method_never_owner(world):
    client, m = world
    librarian_h = headers("agent")
    proposal = ok(
        client.post(
            "/v1/proposals",
            json={
                "kind": "link",
                "payload": {
                    "type": "related_to",
                    "from": {"kind": "message", "id": m["fake-0"]},
                    "to": {"kind": "message", "id": m["fake-1"]},
                    "job_version": "lib-1",
                },
                "origin": "librarian",
            },
            headers=librarian_h,
        )
    )
    assert proposal["status"] == "pending"
    decided = ok(
        client.post(f"/v1/proposals/{proposal['id']}/decide", json={"accept": True})
    )
    assert decided["status"] == "accepted" and decided["decided_at"]
    link_id = decided["result"]["link_id"]
    link = owner("SELECT method,job,job_version FROM tl.links WHERE id=%s", (link_id,))[
        0
    ]
    assert (link["method"], link["job"], link["job_version"]) == (
        "model",
        "librarian",
        "lib-1",
    )
    # Decided proposals stay decided; rejection creates nothing.
    assert (
        client.post(
            f"/v1/proposals/{proposal['id']}/decide", json={"accept": False}
        ).status_code
        == 400
    )
    third = ok(
        client.post(
            "/v1/proposals",
            json={
                "kind": "link",
                "payload": {
                    "type": "related_to",
                    "from": {"kind": "message", "id": m["fake-1"]},
                    "to": {"kind": "message", "id": m["fake-2"]},
                },
            },
            headers=librarian_h,
        )
    )
    rejected = ok(
        client.post(
            f"/v1/proposals/{third['id']}/decide", json={"accept": False, "note": "no"}
        )
    )
    assert rejected["status"] == "rejected" and rejected["result"] == {"note": "no"}
    assert not owner(
        "SELECT 1 FROM tl.links WHERE from_id=%s AND to_id=%s",
        (m["fake-1"], m["fake-2"]),
    )


# --- page_sources stage (BEN-11, BEN-12, QRY-20) -------------------------------


@pytest.fixture()
def page_evidence(client, channel):
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name=PAGE,
                plugin="synthetic",
                is_reference=True,
                record_kind="page_revision",
            ),
        )
    )
    r = ingest(
        client,
        [
            message(
                0,
                external_id="raw",
                content="We spotted a bird in the hayloft at dawn.",
            ),
            message(
                0,
                external_id="late",
                happened_at="2021-06-01T12:00:00+00:00",
                content="Another sighting in the meadow.",
            ),
            message(
                60,
                channel=PAGE,
                external_id="kestrel-page",
                thread={"external_id": "kestrel-page"},
                content="# Barn wildlife\n\nA kestrel nests in the old barn.",
                fields={},
                author=None,
            ),
        ],
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    raw, late, page = mid("raw"), mid("late"), mid("kestrel-page", PAGE)
    owner(
        "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT 'message',%s,m.id,m.version,'source' FROM tl.messages m WHERE m.id=%s",
        (page, raw),
    )
    with connect("app") as c:
        passages.build(c, 100)
    passage = owner("SELECT id FROM tl.passages WHERE message_id=%s", (page,))[0]["id"]
    return dict(raw=raw, late=late, page=page, passage=passage)


def test_ben_11_page_sources_finds_raw_evidence_via_passage(page_evidence):
    raw = f"m:{page_evidence['raw']}"
    plain = recall(OWNER, RecallRequest(query="kestrel barn", k=20, raw_only=True))
    assert raw not in ids_of(plain)
    response = recall(OWNER, RecallRequest(query="kestrel barn", k=20))
    found = {r["record_id"]: r for r in response.records}
    assert f"p:{page_evidence['passage']}" in found
    assert raw in found
    vias = {
        d["detail"]["via"]
        for d in found[raw]["discovery"]
        if d["leg"] == "page_sources"
    }
    assert vias == {f"p:{page_evidence['passage']}"}
    assert found[raw]["role"] == "hit"
    assert stage(response, "page_sources")[0]["status"] == "ok"


def test_qry_20_page_sources_respects_hard_scope(page_evidence):
    other = ids_of(
        recall(
            OWNER,
            RecallRequest(query="kestrel barn", k=20, scope={"channels": [PAGE]}),
        )
    )
    assert f"m:{page_evidence['raw']}" not in other


def test_ben_12_raw_legs_independent_of_bad_pages(page_evidence):
    raw = f"m:{page_evidence['raw']}"
    for request in (
        RecallRequest(query="bird hayloft", k=20, raw_only=True),
        RecallRequest(query="bird hayloft", k=20, hints={"page_sources": False}),
        RecallRequest(query="bird hayloft", k=20),
    ):
        response = recall(OWNER, request)
        assert raw in ids_of(response)
        lexical = next(r for r in response.records if r["record_id"] == raw)[
            "discovery"
        ]
        assert any(d["leg"] == "lexical" for d in lexical)
    raw_only = recall(OWNER, RecallRequest(query="bird hayloft", k=20, raw_only=True))
    assert not stage(raw_only, "page_sources")
    assert all(r["record_id"][0] == "m" for r in raw_only.records)
    off = recall(
        OWNER,
        RecallRequest(query="kestrel barn", k=20, hints={"page_sources": False}),
    )
    assert not stage(off, "page_sources")
    assert raw not in ids_of(off)


# --- migration 0074 cleanup check ---------------------------------------------


def test_claims_commitments_curate_removed(world):
    client, m = world
    for path in ("/v1/claims", "/v1/commitments"):
        assert client.get(path).status_code == 404
    for obj in (
        "tl.claims",
        "tl.claim_sources",
        "tl.claim_relations",
        "tl.commitments",
        "tl.commitment_runs",
        "tl.commitment_assessments",
        "tl.curator_runs",
        "tl.curator_state",
        "api.claims",
        "api.claim_records",
        "api.commitments",
        "api.commitment_records",
    ):
        assert owner("SELECT to_regclass(%s) AS r", (obj,))[0]["r"] is None, obj
    # api.effective (annotations) remains allow-listed and callable.
    res = query_ex(
        f"SELECT * FROM api.effective(ARRAY[{m['fake-0']}]::bigint[])",
        scope=OWNER,
    )
    assert res["row_count"] == 1
