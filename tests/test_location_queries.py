from datetime import datetime, timedelta, timezone

from conftest import ingest, message

from throughline.db import connect


def test_loc_1_loc_2_loc_3_track_and_precedence(client, channel):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ingest(
        client, [message(), message(1, location={"lat": 10, "lon": 10}), message(40000)]
    )
    points = [
        dict(
            at=(start + timedelta(seconds=i)).isoformat(),
            lat=1,
            lon=2,
            accuracy_m=3,
            source="fake-track",
        )
        for i in range(2000)
    ]
    for _ in range(2):
        r = client.post("/v1/ingest/track", json={"points": points})
        assert r.status_code == 200, r.text
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM api.location_track").fetchone()["n"]
            == 2000
        )
        rows = c.execute("SELECT * FROM api.messages ORDER BY happened_at").fetchall()
        assert rows[0]["loc_method"] == "track" and rows[0]["loc_accuracy_m"] == 3
        assert rows[1]["loc_method"] == "source" and rows[1]["lat"] == 10
        assert (
            rows[2]["lat"] is None
            and rows[2]["loc_reason"] == "no_track_point_within_tolerance"
        )
    ingest(client, [message(2000)])
    with connect() as c:
        assert (
            c.execute(
                "SELECT loc_method FROM api.messages WHERE external_id='fake-2000'"
            ).fetchone()["loc_method"]
            == "track"
        )
    assert (
        client.post(
            "/v1/channels",
            json=dict(name="fake.reference", plugin="synthetic", is_reference=True),
        ).status_code
        == 200
    )
    ingest(
        client,
        [
            message(
                5, channel="fake.reference", fields={}, location={"lat": 10, "lon": 10}
            )
        ],
    )
    with connect() as c:
        r = c.execute(
            "SELECT * FROM api.messages WHERE channel='fake.reference'"
        ).fetchone()
        assert r["lat"] is None and r["loc_reason"] == "not_applicable"


def test_loc_4_deterministic_stops(client):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    points = []
    for offset, lat in ((0, 1), (1800, 2)):
        points.extend(
            dict(
                at=(start + timedelta(seconds=offset + i * 60)).isoformat(),
                lat=lat,
                lon=2,
                source="fake-track",
            )
            for i in range(16)
        )
    assert client.post("/v1/ingest/track", json={"points": points}).status_code == 200
    for _ in range(2):
        r = client.post("/v1/track/detect-visits")
        assert r.status_code == 200, r.text
    with connect() as c:
        rows = c.execute("SELECT * FROM api.visits ORDER BY happened_at").fetchall()
        assert len(rows) == 2
        assert all(
            (r["ended_at"] - r["happened_at"]).total_seconds() == 900 for r in rows
        )
        assert [r["lat"] for r in rows] == [1, 2]


def test_qry_2_qry_4_qry_5_qry_6_qry_7_qry_9_functions(client, channel):
    ingest(
        client,
        [
            message(0, ended_at="2020-01-01T00:10:00Z", location={"lat": 1, "lon": 2}),
            message(20),
            message(800),
        ],
    )
    client.post(
        "/v1/entities",
        json={
            "kind": "place",
            "name": "Synthetic place",
            "metadata": {"lat": 1, "lon": 2},
        },
    )
    statements = {
        "SELECT * FROM api.search('orchard')": 3,
        "SELECT * FROM api.near(1,2,100)": 1,
        "SELECT * FROM api.near_place('Synthetic place',100)": 1,
        "SELECT * FROM api.overlapping('2020-01-01T00:00:10Z','2020-01-01T00:01:00Z')": 2,
        "SELECT * FROM api.during(1)": 2,
        "SELECT * FROM api.person('missing')": 0,
        "SELECT * FROM api.person_messages(999)": 0,
        "SELECT * FROM api.follow('message',1,NULL,3)": 0,
    }
    for statement, count in statements.items():
        r = client.post("/v1/sql", json={"statement": statement})
        assert r.status_code == 200, (statement, r.text)
        assert r.json()["row_count"] == count, (statement, r.text)
    catalog = client.get("/v1/catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["channels"][0]["fields"][0]["name"] == "rating"
    assert len(catalog.json()["functions"]) >= 11


def test_qry_2_exhaustive_filtered_keyword_ranking(client, channel):
    rows = [
        message(i, content="orchard orchard" if i % 100 == 0 else "orchard")
        for i in range(1000)
    ]
    ingest(client, rows)
    r = client.post(
        "/v1/sql",
        json={
            "statement": "SELECT id,score FROM api.search('orchard',NULL) WHERE (fields->>'rating')::int % 100=0 ORDER BY score DESC,id LIMIT 10"
        },
    )
    assert r.status_code == 200, r.text
    with connect() as c:
        expected = c.execute(
            "SELECT id,ts_rank_cd(tsv,websearch_to_tsquery('english','orchard')) AS score FROM api.messages WHERE (fields->>'rating')::int % 100=0 ORDER BY score DESC,id LIMIT 10"
        ).fetchall()
        assert r.json()["rows"] == expected


def test_qry_4_qry_12_near_excludes_distant_locations(client, channel):
    ingest(
        client,
        [
            message(0, location={"lat": 12, "lon": 34}),
            message(1, location={"lat": 12.1, "lon": 34.1}),
        ],
    )
    client.post(
        "/v1/entities",
        json={
            "kind": "place",
            "name": "Fake Orchard",
            "metadata": {"lat": 12, "lon": 34},
        },
    ).raise_for_status()
    for statement in (
        "SELECT external_id FROM api.near(12,34,1609)",
        "SELECT external_id FROM api.near_place('Fake Orchard',1609)",
    ):
        response = client.post("/v1/sql", json={"statement": statement})
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [{"external_id": "fake-0"}]
