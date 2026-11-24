"""Contract assertions over wire records and isolated database evidence."""

import copy
import json


def ingest_messages(client, plugin, messages):
    total = {
        "received": 0,
        "inserted": 0,
        "new_versions": 0,
        "unchanged": 0,
        "rejected": [],
    }
    for start in range(0, len(messages), 5000):
        response = client.post(
            "ingest/messages",
            json={"plugin": plugin, "messages": messages[start : start + 5000]},
        )
        response.raise_for_status()
        result = response.json()
        for key in ("received", "inserted", "new_versions", "unchanged"):
            total[key] += result[key]
        total["rejected"].extend(result["rejected"])
    return total


def message_checks(client, manifest, delivered):
    """Exercise validation, versioning, typed views and reordered replay."""

    def ingest(messages):
        return ingest_messages(client, manifest["name"], messages)

    result = ingest(list(reversed(delivered)))
    assert result["inserted"] == result["new_versions"] == 0
    assert result["unchanged"] == len(delivered)
    changed = copy.deepcopy(delivered[0])
    changed["payload"] = {"fake_contract_changed": changed["payload"]}
    assert ingest([changed])["new_versions"] == 1
    assert ingest([changed])["unchanged"] == 1
    # Restore the original shape for subsequent raw-payload reparse checks.
    assert ingest([delivered[0]])["new_versions"] == 1
    for key in (
        "channel",
        "external_id",
        "thread",
        "happened_at",
        "content",
        "payload",
    ):
        invalid = copy.deepcopy(delivered[0])
        del invalid[key]
        assert len(ingest([invalid])["rejected"]) == 1
    catalog = client.get("catalog")
    catalog.raise_for_status()
    for channel in manifest["channels"]:
        assert channel["name"] in json.dumps(catalog.json())
        table = "api.ch_" + channel["name"].replace(".", "_")
        response = client.post(
            "sql", json={"statement": f"SELECT * FROM {table} LIMIT 1"}
        )
        response.raise_for_status()
        row = next((m for m in delivered if m["channel"] == channel["name"]), None)
        assert row is not None, "recorded fixture must cover every declared channel"
        for field in channel.get("fields", []):
            assert field["name"] in response.json()["rows"][0]
            invalid = copy.deepcopy(row)
            invalid["fields"] = {
                **invalid.get("fields", {}),
                field["name"]: ["invalid", "typed", "value"],
            }
            if field["type"] != "json":
                assert len(ingest([invalid])["rejected"]) == 1


def auxiliary_state(env):
    """Compare auxiliary state by natural keys, never generated database IDs."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    settings = conninfo_to_dict(env["THROUGHLINE_DATABASE_URL"])
    if settings.get("host") != "127.0.0.1" or not settings["dbname"].startswith(
        "tl_test_"
    ):
        raise ValueError("not an isolated contract database")
    with psycopg.connect(env["THROUGHLINE_DATABASE_URL"], row_factory=dict_row) as conn:
        messages = {
            r["id"]: [r["channel"], r["external_id"], r["version"]]
            for r in conn.execute(
                "SELECT id,channel,external_id,version FROM api.message_versions"
            )
        }
        entities = {
            r["id"]: [r["kind"], r["external_ref"], r["name"]]
            for r in conn.execute("SELECT * FROM tl.entities")
        }
        people = {r["id"]: r["handle"] for r in conn.execute("SELECT * FROM tl.people")}
        maps = {"message": messages, "entity": entities, "person": people}
        links = []
        for row in conn.execute("SELECT * FROM tl.links WHERE status='active'"):
            links.append(
                [
                    row["type"],
                    row["from_kind"],
                    maps.get(row["from_kind"], {}).get(row["from_id"], row["from_id"]),
                    row["to_kind"],
                    maps.get(row["to_kind"], {}).get(row["to_id"], row["to_id"]),
                    row["confidence"],
                    row["method"],
                    row["evidence"],
                    row["job"],
                    row["job_version"],
                ]
            )
        track = conn.execute(
            "SELECT at,lat,lon,accuracy_m,source FROM tl.location_track ORDER BY source,at"
        ).fetchall()
        identifiers = conn.execute(
            "SELECT system,kind,value,value_norm,status FROM tl.identifiers ORDER BY system,kind,value_norm"
        ).fetchall()
        return json.dumps(
            {
                "links": sorted(links, key=lambda x: json.dumps(x, sort_keys=True)),
                "track": track,
                "people": sorted(people.values()),
                "identifiers": identifiers,
            },
            sort_keys=True,
            default=str,
        )
