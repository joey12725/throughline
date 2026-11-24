import pytest
from throughline_sdk import Client
from throughline_sdk.contract import core_server

from fixtures import corpus, load


def test_ben_3_seeded():
    assert corpus() == corpus()
    assert corpus(1) != corpus(2)


def test_ben_3_qry_12_plg_5_worked_examples():
    with core_server() as (http, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "twin")
        try:
            load(client)
        finally:
            client.close()

        def query(statement):
            response = http.post("sql", json={"statement": statement})
            assert response.status_code == 200, response.text
            return response.json()["rows"]

        near = query(
            "SELECT count(*) n FROM api.near_place('Fake Cloud Orchard',1609) WHERE happened_at >= '2022-01-01' AND happened_at < '2022-07-01'"
        )
        assert near[0]["n"] == 550  # chats, visit, recording, four turns and viewing
        person = query("SELECT id FROM api.people WHERE handle='fake-person-0'")[0][
            "id"
        ]
        day = query(
            f"SELECT * FROM api.person_messages({person}) WHERE happened_at >= '2022-01-01' AND happened_at < '2022-01-02'"
        )
        assert len(day) == 2
        assert {r["channel"] for r in day} == {"twin.fakechat", "twin.transcript"}
        hit = query(
            "SELECT id FROM api.messages WHERE external_id='fake-20-1' AND channel='twin.fakewire'"
        )[0]["id"]
        assert len(query(f"SELECT * FROM api.context({hit},10,5)")) == 16
        recording = query(
            "SELECT id FROM api.messages WHERE external_id='fake-recording-0' AND channel='twin.transcript'"
        )[0]["id"]
        overlap = query(f"SELECT * FROM api.during({recording})")
        assert {"twin.transcript", "twin.fakechat", "twin.media"} <= {
            r["channel"] for r in overlap
        }
        assert len([r for r in overlap if r["channel"] == "twin.transcript"]) == 5
        assert (
            query(
                "SELECT count(*) n FROM api.links WHERE method='source' AND type='part_of'"
            )[0]["n"]
            == 12
        )


@pytest.mark.skip(
    reason="Meaning search and model scoring are outside the current core milestone"
)
def test_qry_12_meaning_and_model_scoring():
    pass
