"""A recorded, synthetic JSONL source; no network access to a source service."""

import json
from pathlib import Path

from throughline_sdk import (
    Channel,
    Checkpoint,
    Declaration,
    Manifest,
    Plugin,
    Record,
    run,
)


class Template(Plugin):
    manifest = Manifest(
        name="template",
        version="0.1.0",
        channels=[
            Channel(
                name="template.chat",
                description="Invented JSONL chat events",
                fields=[
                    {
                        "name": "rating",
                        "type": "integer",
                        "description": "Invented event score",
                    }
                ],
            )
        ],
        settings={"input": Declaration(env="TEMPLATE_INPUT")},
        secrets={"secret": Declaration(env="TEMPLATE_SECRET", required=False)},
        link_types=["part_of"],
    )

    def records(self, cursor, mode):
        # Both modes read the same source shape. A real source uses its historical
        # pagination or incremental endpoint here, keeping normalization shared.
        for index, line in enumerate(
            Path(self.settings["input"]).read_text(encoding="utf-8").splitlines()
        ):
            if index < cursor.get("offset", 0):
                continue
            event = json.loads(line)
            yield dict(
                channel="template.chat",
                external_id=event["id"],
                thread={"external_id": "fake-thread"},
                happened_at=event["at"],
                content=event["text"],
                fields={"rating": event.get("score", 0)},
                payload=event,
            )
            # Optional auxiliary records share the acknowledgment/checkpoint path.
            yield Record(
                kind="link",
                data={
                    "type": "part_of",
                    "from": {"channel": "template.chat", "external_id": event["id"]},
                    "to": {
                        "kind": "work",
                        "external_ref": "fake-story",
                        "name": "Invented Orchard Story",
                    },
                    "method": "source",
                },
            )
            yield Record(
                kind="track",
                data={
                    "at": event["at"],
                    "lat": 12.0,
                    "lon": 34.0,
                    "source": "template",
                },
            )
            # Replay is safe if the process dies after ingest but before this PUT.
            yield Checkpoint(cursor={"offset": index + 1})


def main():
    run(Template)
