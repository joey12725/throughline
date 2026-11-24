"""Seeded public twin: every name and event is invented."""

import os
import random
from datetime import UTC, datetime, timedelta

from throughline_sdk import Channel, Client, Manifest

NAMES = [
    "Zibble Mosskite",
    "Floop Lantern",
    "Quibble Fernwheel",
    "Vexel Cloudspoon",
    "Nimble Starbucket",
    "Wobble Thistle",
    "Pindle Mooncup",
    "Flizzle Pebble",
    "Dorb Sparkleaf",
    "Tindle Snowgear",
    "Yabble Twigboat",
    "Zorple Mistbell",
]


def corpus(seed=47):
    rng = random.Random(seed)
    start = datetime(2022, 1, 1, 12, tzinfo=UTC)
    messages = []
    for day in range(730):
        for turn in range(3):
            person = (day + turn) % 12
            network = ["fakechat", "fakewire"][turn % 2]
            at = start + timedelta(days=day, minutes=turn)
            messages.append(
                dict(
                    channel="twin." + network,
                    external_id=f"fake-{day}-{turn}",
                    thread={"external_id": "fake-thread"},
                    happened_at=at.isoformat(),
                    content=f"Invented orchard {day} with {NAMES[person]}: {rng.choice(['purple kites', 'glass seeds', 'moon jam'])}",
                    author=dict(
                        system=network, kind="account", value=f"fake-person-{person}"
                    ),
                    location=dict(lat=12.0, lon=34.0),
                    payload={"fake": True, "day": day, "turn": turn},
                )
            )
    for recording in range(3):
        at = start + timedelta(days=recording * 100)
        messages.append(
            dict(
                channel="twin.transcript",
                external_id=f"fake-recording-{recording}",
                thread={"external_id": f"fake-recording-{recording}"},
                happened_at=at.isoformat(),
                ended_at=(at + timedelta(minutes=2)).isoformat(),
                content="Invented full recording",
                payload={"fake": True, "recording": recording},
            )
        )
        for turn in range(4):
            messages.append(
                dict(
                    channel="twin.transcript",
                    external_id=f"fake-recording-{recording}-{turn}",
                    thread={"external_id": f"fake-recording-{recording}"},
                    happened_at=(at + timedelta(seconds=turn * 30)).isoformat(),
                    ended_at=(at + timedelta(seconds=(turn + 1) * 30)).isoformat(),
                    content=f"Invented spoken turn {turn}",
                    author=dict(
                        system="fakevoice", kind="account", value=f"fake-person-{turn}"
                    ),
                    payload={"fake": True, "turn": turn},
                )
            )
    for i in range(12):
        at = start + timedelta(days=i * 30)
        messages.append(
            dict(
                channel="twin.media",
                external_id=f"fake-view-{i}",
                thread={"external_id": "fake-media"},
                happened_at=at.isoformat(),
                ended_at=(at + timedelta(hours=1)).isoformat(),
                content=f"Watched invented work {i}",
                payload={"fake": True, "work": i},
            )
        )
        messages.append(
            dict(
                channel="twin.wiki",
                external_id=f"fake-page-{i}",
                thread={"external_id": "fake-wiki"},
                happened_at=at.isoformat(),
                content=f"Invented encyclopedia page about cloud orchard {i}",
                payload={"fake": True},
            )
        )
    return messages


def load(client, seed=47):
    client.register(
        Manifest(
            name="twin",
            version="0.1.0",
            channels=[
                Channel(
                    name="twin." + name,
                    description="Synthetic " + name,
                    is_reference=name == "wiki",
                )
                for name in ["fakechat", "fakewire", "transcript", "media", "wiki"]
            ],
            link_types=["part_of"],
        )
    )
    existing = {p["handle"]: p for p in client.identity("people", method="GET")}
    identifiers = client.registry("identifiers", method="GET")
    known = {(r["system"], r["value"]) for r in identifiers}
    for i, name in enumerate(NAMES):
        handle = f"fake-person-{i}"
        person = existing.get(handle) or client.identity(
            "people", dict(handle=handle, display_name=name)
        )
        for system in ["fakechat", "fakewire", "fakevoice"]:
            if (system, handle) not in known:
                client.registry(
                    "identifiers",
                    dict(
                        person_id=person["id"],
                        system=system,
                        kind="account",
                        value=handle,
                        value_norm=handle,
                    ),
                )
    places = client.request(
        "POST",
        "sql",
        {
            "statement": "SELECT id FROM api.entities WHERE kind='place' AND external_ref='fake-orchard'"
        },
    )["rows"]
    if not places:
        client.request(
            "POST",
            "entities",
            dict(
                kind="place",
                name="Fake Cloud Orchard",
                external_ref="fake-orchard",
                metadata={"lat": 12.0, "lon": 34.0},
            ),
        )
    client.ingest(corpus(seed))
    for i in range(12):
        client.link(
            {
                "type": "part_of",
                "from": {"channel": "twin.media", "external_id": f"fake-view-{i}"},
                "to": {
                    "kind": "work",
                    "external_ref": f"fake-work-{i}",
                    "name": f"Invented Work {i}",
                },
                "method": "source",
            }
        )
    start = datetime(2022, 1, 1, 12, tzinfo=UTC)
    client.track(
        [
            dict(
                at=(start + timedelta(minutes=i)).isoformat(),
                lat=12.0,
                lon=34.0,
                source="twin",
                accuracy_m=3,
            )
            for i in range(31)
        ]
        + [
            dict(
                at=(start + timedelta(hours=2, minutes=i)).isoformat(),
                lat=12.1,
                lon=34.1,
                source="twin",
                accuracy_m=3,
            )
            for i in range(31)
        ]
    )
    client.request("POST", "track/detect-visits")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=47)
    args = parser.parse_args()
    client = Client(
        os.environ["THROUGHLINE_URL"], os.environ["THROUGHLINE_TOKEN"], "twin"
    )
    try:
        load(client, args.seed)
    finally:
        client.close()
