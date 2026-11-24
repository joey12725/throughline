"""T3 Code message attachments (channel t3code.messages).

T3's statev2.sqlite (opened read only) has orchestration_v2_projection_messages
with payload_json.attachments [{type:'image', id:'<threadId>-<uuid>', name,
mimeType, sizeBytes}]; the bytes are <T3 home>/userdata/attachments/<id>.<ext>.
The t3code plugin's external id of a message is its message_id.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import sniff
from . import Candidate, Fetched

CHANNEL = "t3code.messages"


def open_readonly(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(database=uri, uri=True, timeout=30)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError:
        conn = sqlite3.connect(database=uri + "&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _moment(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def attachment_path(attachments_dir, att):
    """The stored file of one attachment, or None."""
    ext = sniff.EXTENSION_OF.get((att.get("mimeType") or "").lower()) or sniff.extension(att.get("name"))
    if ext:
        path = Path(attachments_dir) / f"{att['id']}.{ext}"
        if path.exists():
            return path
    matches = sorted(Path(attachments_dir).glob(f"{att['id']}.*"))
    return matches[0] if matches else None


def candidates_of(message_id, thread_id, created_at, payload, attachments_dir, titles=None):
    out = []
    for i, att in enumerate(payload.get("attachments") or []):
        if not isinstance(att, dict) or not att.get("id"):
            continue
        c = Candidate(
            origin="t3code",
            channel=CHANNEL,
            external_id=message_id,
            ord=i,
            name=att.get("name"),
            mime=sniff.mime_of(att.get("name"), att.get("mimeType")),
            size=att.get("sizeBytes"),
            happened_at=_moment(created_at),
            source_ref={"attachment_id": att["id"], "thread_id": thread_id, "file_name": att.get("name")},
            context={"label": "T3 Code", "thread": (titles or {}).get(thread_id), "author": None, "body": None},
        )
        if att.get("type") != "image" and not sniff.is_image(att.get("name"), att.get("mimeType")):
            c.skip = "not_image"
        else:
            path = attachment_path(attachments_dir, att)
            if path is None:
                c.skip = "file_missing"
            else:
                c.route = "file"
                c.source_ref["path"] = str(path)
        out.append(c)
    return out


def discover(conn, settings, *, limit=None):
    db = settings.t3_home / "userdata" / "statev2.sqlite"
    attachments_dir = settings.t3_home / "userdata" / "attachments"
    if not db.exists():
        return
    lite = open_readonly(db)
    try:
        titles = {}
        try:
            for row in lite.execute("SELECT thread_id, title FROM orchestration_v2_projection_threads"):
                titles[row["thread_id"]] = row["title"]
        except sqlite3.Error:
            pass
        rows = lite.execute(
            "SELECT message_id, thread_id, created_at, payload_json FROM orchestration_v2_projection_messages"
            " WHERE payload_json LIKE '%\"attachments\":[{%' AND NOT streaming ORDER BY created_at DESC"
        ).fetchall()
    finally:
        lite.close()
    found = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        found.extend(
            candidates_of(row["message_id"], row["thread_id"], row["created_at"], payload, attachments_dir, titles)
        )
    if conn is not None and found:
        ids = sorted({c.external_id for c in found})
        known = {
            r["external_id"]
            for r in conn.execute(
                "SELECT m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id"
                " WHERE c.name=%s AND m.superseded_by IS NULL AND m.external_id = ANY(%s)",
                (CHANNEL, ids),
            )
        }
        for c in found:
            c.message_known = c.external_id in known
    for n, c in enumerate(found):
        if limit and n >= limit:
            return
        yield c


class Resolver:
    def __init__(self, settings):
        self.settings = settings

    def close(self):
        pass

    def fetch(self, c):
        path = c.source_ref.get("path")
        if not path:
            return Fetched("not_found", detail="no attachment file")
        try:
            return Fetched("ok", Path(path).read_bytes())
        except OSError as error:
            return Fetched("not_found", detail=type(error).__name__)
