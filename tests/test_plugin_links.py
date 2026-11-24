from conftest import ingest, message


def test_plg_4_plg_5_source_link_replay(client, channel):
    link = {
        "type": "part_of",
        "from": {"channel": channel, "external_id": "fake-0"},
        "to": {"kind": "work", "external_ref": "fake-work", "name": "Invented work"},
        "method": "source",
    }
    assert ingest(client, [message()], links=[link]).status_code == 200
    assert ingest(client, [message()], links=[link]).status_code == 200
    first = client.post("/v1/links", json=link).json()
    second = client.post("/v1/links", json=link).json()
    assert first["id"] == second["id"]
    rows = client.post("/v1/sql", json={"statement": "SELECT * FROM api.links"}).json()[
        "rows"
    ]
    assert len(rows) == 1
    assert rows[0]["method"] == "source"


def test_plg_5_missing_link_message_status_and_detail_are_stable(client, channel):
    # The Beeper plugin drops a reply link only when the core answers with
    # exactly this status and detail, for a plugin token delivering a batch.
    from throughline.db import create_token

    plugin = {
        "Authorization": "Bearer "
        + create_token("synthetic-plugin", "plugin", "synthetic")["token"]
    }
    link = {
        "type": "reply_to",
        "from": {"channel": channel, "external_id": "fake-0"},
        "to": {"channel": channel, "external_id": "fake-absent"},
    }
    for headers in (plugin, None):
        batch = client.post(
            "/v1/ingest/messages",
            json=dict(plugin="synthetic", messages=[message()], links=[link]),
            headers=headers,
        )
        assert batch.status_code == 400, batch.text
        assert batch.json() == {"detail": "link message does not exist", "code": "link_message_missing"}
        single = client.post("/v1/links", json=link, headers=headers)
        assert single.status_code == 400, single.text
        assert single.json() == {"detail": "link message does not exist", "code": "link_message_missing"}
    # The refused batch stored nothing, its message included.
    rows = client.post(
        "/v1/sql", json={"statement": "SELECT count(*) AS n FROM api.messages"}
    ).json()["rows"]
    assert rows[0]["n"] == 0
