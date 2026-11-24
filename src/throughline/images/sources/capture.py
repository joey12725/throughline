"""Screenshots from the Throughline Companion (channel capture.screens).

The desktop's capture connector uploads each screenshot to Immich itself
(file name tl-capture-<device>-<time>) and records its Immich asset id in
the message's fields. This source adopts those assets: it reads the
original back from Immich once (for the content hash), stores it as a
`screenshot` linked to its record with role `self` (the record is the
screenshot), and the upload stage finds Immich already has it (a duplicate
by checksum), so only the organizing (album "Source · Screen capture",
tags) and enrichment (OCR, vectors, NSFW) happen. `native` skips these
files so they never become camera-roll photos.
"""

from datetime import datetime

from . import Candidate, Fetched

CHANNEL = "capture.screens"
PREFIX = "tl-capture-"


def discover(conn, settings, *, limit=None):
    if conn is None:
        return
    rows = conn.execute(
        "SELECT m.external_id, m.happened_at, m.fields, t.title AS thread FROM tl.messages m"
        " JOIN tl.channels c ON c.id=m.channel_id LEFT JOIN tl.threads t ON t.id=m.thread_id"
        " WHERE c.name=%s AND m.superseded_by IS NULL AND m.fields->>'immich_asset_id' IS NOT NULL"
        " ORDER BY m.happened_at DESC",
        (CHANNEL,),
    ).fetchall()
    for n, row in enumerate(rows):
        if limit and n >= limit:
            return
        fields = row["fields"] or {}
        happened = row["happened_at"]
        yield Candidate(
            origin="capture",
            channel=CHANNEL,
            external_id=row["external_id"],
            role="self",
            name=f"{PREFIX}{fields.get('device') or 'desktop'}-{happened:%Y%m%dT%H%M%S}.jpg"
            if isinstance(happened, datetime)
            else None,
            mime="image/jpeg",
            size=fields.get("bytes"),
            width=fields.get("width"),
            height=fields.get("height"),
            happened_at=happened,
            source_ref={"immich_id": fields["immich_asset_id"], "app": fields.get("app")},
            context={"label": "Screen capture", "thread": row.get("thread"), "author": None, "body": None},
            route="immich",
            message_known=True,
        )


class Resolver:
    def __init__(self, settings):
        self.settings = settings
        self.client = None

    def close(self):
        if self.client is not None:
            self.client.http.close()

    def fetch(self, c):
        from ..immich import Immich, ImmichError

        try:
            if self.client is None:
                self.client = Immich(self.settings.immich_url, self.settings.immich_api_key)
            return Fetched("ok", self.client.original(c.source_ref["immich_id"]))
        except ImmichError as error:
            return Fetched("not_found" if error.status == 404 else "failed", detail=str(error)[:200])
