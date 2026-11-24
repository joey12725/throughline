"""Chat attachments (channels chat.*, plugin beeper).

Discovery reads fields.attachments of current message versions, and the raw
payload for the Beeper ids (metadata_json.beeper {account_id, chat_id,
message_id}, or the live row's accountID/chatID/id). Bytes:

1. a Discord CDN url whose `ex=` expiry is still ahead is tried once, locally;
2. otherwise the Beeper instance that holds the chats (its token stays on that
   host): `throughline lib images beeper-requests`
   writes a request list, images/beeper_fetch.py runs next to that instance and
   writes manifest.jsonl + <sha256>.<ext>, which is copied back to
   BEEPER_MEDIA_IMPORT_DIR and read here.

Never taken: chat.snapchat, and messages or chats marked as expiring or
ephemeral (disappearing media).

Shared posts: when the message's only text is a link to a post or reel
(images/shared.py: Instagram p/reel/reels/tv, TikTok video, YouTube Shorts,
X/Twitter status), its image is the post's cover: role 'preview' and
source_ref.shared_url.
"""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .. import shared, sniff
from . import Candidate, Fetched

LABELS = {
    "chat.discord": "Discord",
    "chat.instagram": "Instagram",
    "chat.sms": "SMS",
    "chat.reddit": "Reddit",
    "chat.snapchat": "Snapchat",
}
EXCLUDED_CHANNELS = {"chat.snapchat"}

QUERY = """
SELECT c.name AS channel, m.external_id, m.happened_at, m.fields->'attachments' AS attachments,
       m.author_handle, p.display_name AS person, t.title AS thread_title, left(m.content, 200) AS body,
       CASE WHEN length(m.content) <= 3000 THEN m.content END AS text,
       r.payload->>'metadata_json' AS meta, r.payload->>'sender_name' AS sender_name,
       r.payload->>'chatID' AS live_chat, r.payload->>'accountID' AS live_account, r.payload->>'id' AS live_id,
       r.payload->>'sortKey' AS live_sort
  FROM tl.messages m
  JOIN tl.channels c ON c.id = m.channel_id
  LEFT JOIN tl.raw_payloads r ON r.message_id = m.id
  LEFT JOIN tl.threads t ON t.id = m.thread_id
  LEFT JOIN tl.people p ON p.id = m.person_id
 WHERE c.plugin = 'beeper' AND m.superseded_by IS NULL
   AND jsonb_typeof(m.fields->'attachments') = 'array'
   AND jsonb_array_length(m.fields->'attachments') > 0
 ORDER BY m.happened_at DESC
"""


def cdn_expiry(url):
    """Expiry (epoch seconds) of a signed Discord CDN url, or None."""
    try:
        query = parse_qs(urlparse(url).query)
        return int(query["ex"][0], 16) if "ex" in query else None
    except (ValueError, KeyError, IndexError):
        return None


def ephemeral(meta):
    """True when the message or its chat says it expires (disappearing media)."""
    if not isinstance(meta, dict):
        return False
    policy = meta.get("collection_policy") or {}
    if policy.get("chat_expiry_seconds"):
        return True
    for key, value in meta.items():
        lower = key.lower()
        if value and ("expir" in lower or "ephemeral" in lower or "viewonce" in lower or "disappear" in lower):
            return True
    return False


def beeper_ids(external_id, meta, row=None):
    """{'account', 'chat', 'message', 'sort_key'} or None."""
    row = row or {}
    meta = meta or {}
    beeper = meta.get("beeper") or {}
    chat = beeper.get("chat_id") or meta.get("chatID") or row.get("live_chat")
    message = beeper.get("message_id") or (meta.get("id") if chat else None) or row.get("live_id")
    account = beeper.get("account_id") or meta.get("accountID") or row.get("live_account")
    if not chat and external_id.startswith("beeper:"):
        parts = external_id.split(":")
        if len(parts) == 4:
            account, chat, message = (unquote(p) for p in parts[1:])
    if not chat or not message:
        return None
    return {
        "account": account,
        "chat": chat,
        "message": str(message),
        "sort_key": meta.get("sortKey") or row.get("live_sort"),
    }


def manifest_key(c):
    return (c.channel, c.external_id, c.ord)


def read_manifest(directory):
    """{(channel, external_id, ord): manifest row} of a beeper_fetch.py output directory."""
    rows = {}
    if not directory:
        return rows
    path = Path(directory) / "manifest.jsonl"
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = (row.get("channel"), row.get("external_id"), int(row.get("ord") or 0))
        if key not in rows or row.get("status") == "ok":
            rows[key] = row
    return rows


def candidates_of(row, *, now=None, imported=None):
    """Candidates of one message row (pure; used by discover and the tests)."""
    now = now or time.time()
    meta = {}
    if row.get("meta"):
        try:
            meta = json.loads(row["meta"])
        except (TypeError, ValueError):
            meta = {}
    ids = beeper_ids(row["external_id"], meta, row)
    author = row.get("person") or row.get("sender_name") or row.get("author_handle")
    context = {
        "label": LABELS.get(row["channel"], row["channel"]),
        "thread": row.get("thread_title"),
        "author": author,
        "body": row.get("body"),
    }
    share = shared.shared_post(row["text"] if "text" in row else row.get("body"))
    out = []
    for i, a in enumerate(row.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        name = a.get("fileName") or a.get("filename") or a.get("title")
        mime = a.get("mimeType") or a.get("content_type")
        typ = a.get("type")
        image = sniff.is_image(name, mime) or (typ == "img" and not mime)
        c = Candidate(
            origin="beeper",
            channel=row["channel"],
            external_id=row["external_id"],
            ord=i,
            name=name,
            mime=sniff.mime_of(name, mime),
            size=a.get("fileSize") or a.get("fileSizeBytes") or a.get("size"),
            width=a.get("width"),
            height=a.get("height"),
            happened_at=row.get("happened_at"),
            context=context,
            message_known=True,
        )
        ref = {}
        if a.get("url"):
            ref["url"] = a["url"]
        if a.get("id"):
            ref["attachment_id"] = str(a["id"])
        if ids:
            ref.update(chat_id=ids["chat"], message_id=ids["message"])
            if ids.get("account"):
                ref["account_id"] = ids["account"]
        if name:
            ref["file_name"] = name
        if share and image:
            c.role = "preview"
            ref["shared_url"] = share[1]
            ref["shared_network"] = share[0]
        c.source_ref = ref
        if row["channel"] in EXCLUDED_CHANNELS or ephemeral(meta) or ephemeral(a):
            c.skip = "ephemeral_excluded"
        elif typ == "video" or (mime or "").startswith("video/"):
            c.skip = "video"
        elif a.get("isSticker"):
            c.skip = "sticker"
        elif not image:
            c.skip = "not_image"
        else:
            expiry = cdn_expiry(ref.get("url")) if ref.get("url") else None
            if ref.get("url") and (expiry is None or expiry > now):
                c.route = "url"
            elif ids:
                c.route = "export"
                if imported is not None and manifest_key(c) not in imported:
                    c.skip = "needs_beeper_export"
            elif ref.get("url"):
                c.skip = "expired_url"
            else:
                c.skip = "no_source"
        out.append(c)
    return out


def discover(conn, settings, *, limit=None, now=None):
    imported = read_manifest(settings.beeper_import_dir)
    count = 0
    # Read every row before yielding: the caller commits per stored asset, and
    # a commit would close a server-side cursor mid-scan (about 18k rows here).
    with conn.cursor() as cursor:
        cursor.execute(QUERY)
        rows = cursor.fetchall()
    for row in rows:
        for c in candidates_of(row, now=now, imported=imported):
            yield c
            count += 1
            if limit and count >= limit:
                return


def requests(candidates):
    """The request list beeper_fetch.py reads."""
    return [
        {
            "channel": c.channel,
            "external_id": c.external_id,
            "chat_id": c.source_ref["chat_id"],
            "message_id": c.source_ref["message_id"],
            "ord": c.ord,
            "file_name": c.name,
        }
        for c in candidates
        if c.source_ref.get("chat_id") and c.skip in (None, "needs_beeper_export")
        and c.route in ("export", "url")
    ]


class Resolver:
    def __init__(self, settings, transport=None):
        self.dir = settings.beeper_import_dir
        self.manifest = read_manifest(self.dir)
        self.http = httpx.Client(timeout=60, transport=transport, follow_redirects=True)

    def close(self):
        self.http.close()

    def _url(self, url):
        try:
            response = self.http.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.content:
            return None
        return response.content

    def fetch(self, c):
        ref = c.source_ref
        if c.route == "url" and ref.get("url"):
            data = self._url(ref["url"])
            if data:
                return Fetched("ok", data)
        row = self.manifest.get(manifest_key(c))
        if row is None:
            if not ref.get("chat_id"):
                return Fetched("expired", detail="cdn url refused and no beeper ids")
            return Fetched("pending_export", detail="not in BEEPER_MEDIA_IMPORT_DIR")
        if row.get("status") != "ok" or not row.get("file"):
            return Fetched("not_found" if row.get("status") != "ephemeral" else "filtered", detail=row.get("status"))
        try:
            data = (Path(self.dir) / row["file"]).read_bytes()
        except OSError as error:
            return Fetched("not_found", detail=type(error).__name__)
        return Fetched("ok", data, row.get("mime"))
