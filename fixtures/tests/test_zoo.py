import json
from datetime import datetime

from throughline_sdk.contract import core_server

from fixtures.zoo import ANCHOR, FIELD_TYPES, PLUGIN_OF, corpus, load

SCALE = 0.01


def test_zoo_seeded_and_shaped():
    data = corpus(47, SCALE)
    assert json.dumps(data, sort_keys=True) == json.dumps(
        corpus(47, SCALE), sort_keys=True
    )
    assert json.dumps(data["messages"]) != json.dumps(corpus(2, SCALE)["messages"])
    checks = {
        "text": lambda v: isinstance(v, str),
        "integer": lambda v: type(v) is int,
        "number": lambda v: type(v) in (int, float),
        "boolean": lambda v: type(v) is bool,
        "json": lambda v: True,
        "timestamp": lambda v: datetime.fromisoformat(v).utcoffset() is not None,
    }
    seen = set()
    for m in data["messages"] + data["revisions"]:
        declared = FIELD_TYPES[m["channel"]]
        for name, value in m["fields"].items():
            assert checks[declared[name]](value), (m["channel"], name, value)
        assert m["content"].strip() and datetime.fromisoformat(m["happened_at"])
        if "ended_at" in m:
            assert m["ended_at"] >= m["happened_at"], m["external_id"]
        key = (m["channel"], m["external_id"], "expected_version" in m)
        assert key not in seen, key
        seen.add(key)
    channels = {m["channel"] for m in data["messages"]}
    # Every mirrored channel is covered except the ones left empty on purpose.
    assert set(PLUGIN_OF) - channels == {
        "health.oura.session",
        "youtube.subscriptions",
        "youtube.transcripts",
        "youtube.watch",
        "youtube.watch_later",
    }
    future = [m for m in data["messages"] if m["happened_at"] > ANCHOR.isoformat()]
    assert future and {m["channel"] for m in future} == {"calendar.events"}
    chat = [m for m in data["messages"] if m["channel"].startswith("chat.")]
    assert {m["fields"]["direction"] for m in chat} == {"incoming", "outgoing"}
    assert {m["fields"]["conversation_kind"] for m in chat} >= {"dm", "group"}
    assert any(m.get("images") for m in data["messages"])
    assert all(-90 <= p["lat"] <= 90 for p in data["track"])


def test_zoo_loads_and_replays():
    with core_server() as (http, env):
        url, token = env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"]
        first = load(url, token, 47, SCALE)

        def query(statement):
            response = http.post("sql", json={"statement": statement})
            assert response.status_code == 200, response.text
            return response.json()["rows"]

        def state():
            return {
                name: query(f"SELECT count(*) n FROM api.{name}")[0]["n"]
                for name in (
                    "message_versions",
                    "people",
                    "identifiers",
                    "entities",
                    "links",
                    "facts",
                    "relations",
                    "annotations",
                    "coverage_gaps",
                    "location_track",
                )
            }

        before = state()
        # Every message once, the earlier page revisions and the detected visits.
        assert before["message_versions"] == (
            first["messages"]
            + sum(c["new_versions"] for c in first["ingest"].values())
            + query("SELECT count(*) n FROM api.visits")[0]["n"]
        )
        assert first["provenance"]["unresolved"] == 0
        assert set(first["links"]) <= {"created", "existing"}
        again = load(url, token, 47, SCALE)
        assert state() == before
        assert all(
            c["inserted"] == 0 and c["new_versions"] == 0
            for c in again["ingest"].values()
        )
        assert again["annotations_created"] == 0
        assert query("SELECT count(*) n FROM api.people WHERE is_owner")[0]["n"] == 1
        assert query("SELECT count(*) n FROM api.visits")[0]["n"] >= 2
        assert query("SELECT count(*) n FROM api.coverage_gaps")[0]["n"] == 2
        plugins = {p["name"]: p for p in http.get("status").json()["plugins"]}
        assert plugins["youtube"]["last_success_at"] is None
        assert plugins["research"]["last_error"]
        assert (
            query(
                "SELECT count(*) n FROM api.messages WHERE channel='location.timeline' AND lat IS NOT NULL"
            )[0]["n"]
            > 100
        )
        assert (
            query(
                "SELECT count(*) n FROM api.message_versions WHERE channel='gbrain.pages' AND version=2"
            )[0]["n"]
            >= 1
        )
