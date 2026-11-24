from conftest import ingest, message

from throughline.db import connect, create_token


def test_idn_1_idn_2_attach_late_and_uniqueness(client, channel):
    p = client.post(
        "/v1/identity/people", json={"handle": "fake-owner", "is_owner": True}
    ).json()
    q = client.post("/v1/identity/people", json={"handle": "fake-peer"}).json()
    assert (
        client.post(
            "/v1/identity/people", json={"handle": "fake-other-owner", "is_owner": True}
        ).status_code
        == 400
    )
    assert ingest(client, [message()]).json()["inserted"] == 1
    account = client.get("/v1/identity/unclaimed").json()[0]
    assert (
        client.post(
            "/v1/identity/attach",
            json={"identifier_id": account["id"], "person_id": p["id"]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/identity/attach",
            json={"identifier_id": account["id"], "person_id": q["id"]},
        ).status_code
        == 400
    )
    ingest(client, [message(1)])
    with connect() as c:
        assert [
            r["person_id"] for r in c.execute("SELECT person_id FROM api.messages")
        ] == [p["id"], p["id"]]
    assert (
        client.get("/v1/person", params={"q": "fake-account"}).json()["row_count"] == 1
    )


def registry(c):
    result = {}
    for t in ("people", "identifiers", "person_names", "facts", "relations"):
        result[t] = [
            r["j"]
            for r in c.execute(f"SELECT to_jsonb(t)::text j FROM tl.{t} t ORDER BY id")
        ]
    # Attribution is part of the registry state a split must restore.
    result["messages"] = [
        (r["id"], r["person_id"])
        for r in c.execute("SELECT id,person_id FROM tl.messages ORDER BY id")
    ]
    return result


def test_idn_5_merge_split_exact_and_owner_precedence(client, channel):
    p = client.post(
        "/v1/identity/people", json={"handle": "fake-a", "is_owner": True}
    ).json()["id"]
    q = client.post("/v1/identity/people", json={"handle": "fake-b"}).json()["id"]
    ingest(client, [message()])
    identifier = client.get("/v1/identity/unclaimed").json()[0]["id"]
    client.post(
        "/v1/identity/attach", json={"identifier_id": identifier, "person_id": p}
    )
    client.post("/v1/registry/names", json={"person_id": p, "name": "Synthetic Alias"})
    client.post("/v1/registry/fact-keys", json={"key": "synthetic_fact"})
    client.post(
        "/v1/registry/facts",
        json={"person_id": p, "key": "synthetic_fact", "value": {"fake": True}},
    )
    client.post(
        "/v1/registry/relation-kinds", json={"kind": "parent", "inverse": "child"}
    )
    client.post(
        "/v1/registry/relations", json={"from_id": p, "to_id": q, "kind": "parent"}
    )
    with connect() as c:
        before = registry(c)
    merge = client.post("/v1/identity/merge", json={"source": p, "target": q})
    assert merge.status_code == 200, merge.text
    r = client.post("/v1/identity/split", json={"event_id": merge.json()["id"]})
    assert r.status_code == 200, r.text
    with connect() as c:
        assert registry(c) == before
    agent = create_token("fake-agent", "agent")["token"]
    client.headers["Authorization"] = "Bearer " + agent
    for operation in ("detach", "ignore"):
        assert (
            client.post(
                "/v1/identity/" + operation, json={"identifier_id": identifier}
            ).status_code
            == 400
        )
    assert (
        client.post("/v1/identity/merge", json={"source": p, "target": q}).status_code
        == 400
    )


def test_idn_6_ignored_accounts(client, channel):
    ingest(client, [message()])
    i = client.get("/v1/identity/unclaimed").json()[0]["id"]
    assert (
        client.post("/v1/identity/ignore", json={"identifier_id": i}).status_code == 200
    )
    assert client.get("/v1/identity/unclaimed").json() == []


def test_idn_7_allow_list_and_inverse_relations(client):
    a = client.post("/v1/identity/people", json={"handle": "fake-a"}).json()["id"]
    b = client.post("/v1/identity/people", json={"handle": "fake-b"}).json()["id"]
    assert (
        client.post(
            "/v1/registry/facts",
            json={"person_id": a, "key": "unlisted", "value": "fake"},
        ).status_code
        == 400
    )
    assert (
        client.post("/v1/registry/fact-keys", json={"key": "birthday"}).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/registry/facts",
            json={"person_id": a, "key": "birthday", "value": "2000-01-01"},
        ).status_code
        == 200
    )
    client.post(
        "/v1/registry/relation-kinds", json={"kind": "parent", "inverse": "child"}
    )
    assert (
        client.post(
            "/v1/registry/relations", json={"from_id": a, "to_id": b, "kind": "parent"}
        ).status_code
        == 200
    )
    with connect() as c:
        r = c.execute("SELECT * FROM api.relations ORDER BY kind").fetchall()
        assert [(x["from_id"], x["to_id"], x["kind"]) for x in r] == [
            (b, a, "child"),
            (a, b, "parent"),
        ]


def test_lnk_1_lnk_2_lnk_3_plg_5_placeholders_follow_merge(client, channel):
    link = {
        "type": "part_of",
        "from": {"channel": channel, "external_id": "fake-0"},
        "to": {
            "kind": "synthetic_work",
            "external_ref": "fake-work",
            "name": "Synthetic work",
        },
        "method": "source",
    }
    r = ingest(client, [message()], links=[link])
    assert r.status_code == 200, r.text
    with connect() as c:
        entity = c.execute("SELECT * FROM tl.entities").fetchone()
        assert entity["is_placeholder"]
        mid = c.execute("SELECT id FROM api.messages").fetchone()["id"]
        assert (
            c.execute("SELECT * FROM api.follow('message',%s)", (mid,)).fetchone()["id"]
            == entity["id"]
        )
        assert (
            c.execute(
                "SELECT * FROM api.follow('entity',%s)", (entity["id"],)
            ).fetchone()["id"]
            == mid
        )
    real = client.post(
        "/v1/entities",
        json={
            "kind": "synthetic_work",
            "name": "Real synthetic work",
            "external_ref": "fake-real",
        },
    ).json()
    assert (
        client.post(
            "/v1/entities/merge", json={"source": entity["id"], "target": real["id"]}
        ).status_code
        == 200
    )
    with connect() as c:
        rows = c.execute("SELECT * FROM api.links").fetchall()
        assert (
            len(rows) == 1
            and rows[0]["to_id"] == real["id"]
            and rows[0]["method"] == "source"
        )


def test_idn_6_ignore_thread(client, channel):
    ingest(client, [message()])
    with connect() as c:
        tid = c.execute("SELECT id FROM tl.threads").fetchone()["id"]
    r = client.post("/v1/identity/ignore", json={"thread_id": tid})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["ignored"] is True


def test_qry_6_recipient_links(client, channel):
    person = client.post(
        "/v1/identity/people", json={"handle": "fake-recipient"}
    ).json()["id"]
    ingest(
        client,
        [message()],
        links=[
            {
                "type": "recipient",
                "from": {"channel": channel, "external_id": "fake-0"},
                "to": {"kind": "person", "id": person},
            }
        ],
    )
    r = client.post(
        "/v1/sql", json={"statement": f"SELECT * FROM api.person_messages({person})"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 1


def test_idn_5_split_cannot_overwrite_later_owner_assertion(client, channel):
    owner_auth = client.headers["Authorization"]
    agent = create_token("synthetic-agent", "agent")["token"]
    client.headers["Authorization"] = "Bearer " + agent
    a = client.post("/v1/identity/people", json={"handle": "fake-first"}).json()["id"]
    b = client.post("/v1/identity/people", json={"handle": "fake-second"}).json()["id"]
    client.headers["Authorization"] = owner_auth
    ingest(client, [message()])
    identifier = client.get("/v1/identity/unclaimed").json()[0]["id"]
    client.headers["Authorization"] = "Bearer " + agent
    client.post(
        "/v1/identity/attach", json={"identifier_id": identifier, "person_id": a}
    )
    event = client.post("/v1/identity/merge", json={"source": a, "target": b}).json()[
        "id"
    ]
    client.headers["Authorization"] = owner_auth
    assert (
        client.post(
            "/v1/identity/attach", json={"identifier_id": identifier, "person_id": b}
        ).status_code
        == 200
    )
    client.headers["Authorization"] = "Bearer " + agent
    assert (
        client.post("/v1/identity/split", json={"event_id": event}).status_code == 400
    )
