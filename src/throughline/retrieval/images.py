"""images[] of messages in responses: one api.assets_of statement per call.

    found = images.of(run, [message ids], include_nsfw=False)
    # {message_id: [{asset_id, kind, tags, people, place, location_source,
    #                taken_at, ocr, caption, width, height, nsfw, thumb_url,
    #                content_url}]}

nsfw: the image is not safe (score at or above the owner's threshold, or not
scored yet); it is listed under nsfw_mode blur or show, or with include_nsfw.
Under blur its thumb_url and content_url serve a blurred image; a flagged
image that is not blurred gets URLs with include_nsfw=true.
location_source: 'exif' (GPS of the file), 'track' (the owner's location
history at taken_at) or null.

run(statement, cap) -> rows runs on the reader path under the request's
scope, so the asker gate decides which messages give images and whether
people are named. A failure gives no images and never fails the response.
Messages without images are absent from the result (callers add no key).
"""

import logging

from ..agent_view import local_time
from .scope_sql import lit

log = logging.getLogger("throughline.retrieval.images")

MAX_MESSAGES = 5000
MAX_PER_MESSAGE = 12


def image(row):
    asset_id = row["asset_id"]
    media = bool(row.get("has_media"))
    flagged = bool(row.get("nsfw"))
    query = "?include_nsfw=true" if flagged and not row.get("blurred") else ""
    return dict(
        asset_id=asset_id,
        kind=row.get("kind"),
        tags=list(row.get("tags") or []),
        people=list(row.get("people") or []),
        place=row.get("place"),
        location_source=row.get("location_source"),
        taken_at=local_time(row.get("taken_at")),
        ocr=row.get("ocr") or None,
        caption=row.get("caption"),
        width=row.get("width"),
        height=row.get("height"),
        nsfw=flagged,
        thumb_url=f"/v1/assets/{asset_id}/thumb{query}" if media else None,
        content_url=f"/v1/assets/{asset_id}/content{query}" if media else None,
    )


def of(run, message_ids, include_nsfw=False):
    ids = sorted({int(i) for i in message_ids if i is not None})[:MAX_MESSAGES]
    if not ids:
        return {}
    try:
        rows = run(
            "SELECT * FROM api.assets_of("
            + lit(ids, "bigint[]")
            + ","
            + lit(bool(include_nsfw))
            + ")",
            len(ids) * MAX_PER_MESSAGE,
        )
    except Exception as exc:
        log.warning("images unavailable: %s", type(exc).__name__)
        return {}
    found = {}
    for row in rows:
        items = found.setdefault(row["message_id"], [])
        if len(items) < MAX_PER_MESSAGE:
            items.append(image(row))
    return found


def attach(run, items, include_nsfw=False, key="message_id"):
    """Add images to every dict in items whose key names a message with
    images (one statement for all of them). Returns items."""
    items = [i for i in items if isinstance(i, dict)]
    found = of(run, [i.get(key) for i in items if isinstance(i.get(key), int)], include_nsfw)
    if found:
        for item in items:
            value = found.get(item.get(key))
            if value:
                item["images"] = [dict(v) for v in value]
    return items


def reader(scope, token=None):
    """run(statement, cap) on the reader path under scope."""
    from ..query import query_ex

    def run(statement, cap):
        return query_ex(
            statement, cap, scope=scope, token_row=token, materialize=False
        )["rows"]

    return run
