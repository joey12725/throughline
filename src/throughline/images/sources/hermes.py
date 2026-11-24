"""Hermes image parts (channels hermes.messages / hermes.automation).

Streams the Hermes export (JSON lines, optionally gzip; kinds run, session,
message, run_end) without loading it: message payload content that is a list
of parts carries {type: 'image_url', image_url: {url: 'data:image/...;base64,'}}.
The exporter cuts every string to HERMES_MAX_TEXT characters and lists the
cut paths in the record's `truncated`; such an image (or one whose base64 or
image structure does not survive decoding) is counted as truncated and needs
a re-export with a larger HERMES_MAX_TEXT.

External ids follow the hermes plugin: `<session>:<message_uid>`, else
`<session>:<row id, 10-digit zero padded>`; the channel is hermes.automation
for sessions whose source is in HERMES_AUTOMATION_SOURCES (default cron) or
whose id starts with cron_, else hermes.messages.
"""

import gzip
import json
from datetime import UTC, datetime

from .. import sniff
from . import Candidate, Fetched

MESSAGES = "hermes.messages"
AUTOMATION = "hermes.automation"
IMAGE_PART_TYPES = ("image_url", "input_image", "image")


def message_external_id(session_id, message_id, uid=None):
    if uid:
        return f"{session_id}:{uid}"
    return f"{session_id}:{int(message_id):010d}"


def channel_for(session_id, source, automation_sources=("cron",)):
    source = str(source or "unknown").strip().lower() or "unknown"
    if source in automation_sources or str(session_id).startswith("cron_"):
        return AUTOMATION
    return MESSAGES


def _open(path):
    path = str(path)
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def image_parts(content, path="content"):
    """(json path, part) of every image part, recursively."""
    if isinstance(content, dict):
        if content.get("type") in IMAGE_PART_TYPES:
            yield path, content
            return
        for k, v in content.items():
            yield from image_parts(v, f"{path}.{k}")
    elif isinstance(content, list):
        for i, v in enumerate(content):
            yield from image_parts(v, f"{path}[{i}]")


def part_url(part):
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    return url or part.get("url") or part.get("source") or part.get("image")


def _moment(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if seconds > 1e12:
        seconds /= 1000
    return datetime.fromtimestamp(seconds, UTC)


def candidates_of(record):
    """Candidates of one message record (channel resolved later); each carries `.uri`."""
    payload = record.get("payload") or {}
    sid = str(record.get("session_id"))
    cut = record.get("truncated") or []
    out = []
    for n, (path, part) in enumerate(image_parts(payload.get("content"))):
        url = part_url(part)
        c = Candidate(
            origin="hermes",
            channel=MESSAGES,
            external_id=message_external_id(sid, record["id"], payload.get("message_uid")),
            ord=n,
            role="inline",
            happened_at=_moment(payload.get("timestamp")),
            source_ref={"session_id": sid, "message_id": int(record["id"]), "part": path},
            context={"label": "Hermes", "thread": None, "author": payload.get("role"), "body": None},
        )
        c.uri = None
        if not isinstance(url, str):
            c.skip = "no_url"
        elif url.startswith("data:"):
            was_cut = any(p == path or p.startswith(path + ".") or p.startswith(path + "[") for p in cut)
            try:
                data, mime = sniff.decode_data_uri(url, cut=was_cut)
                c.mime, c.size = mime, len(data)
                c.route = "data_uri"
                c.uri = url
            except sniff.DataUriError as error:
                c.skip = error.reason
        else:
            c.skip = "remote_url"
            c.source_ref["url"] = url[:500]
        out.append(c)
    return out


def scan(path, automation_sources=("cron",)):
    """(candidates, stats) over the whole export, streamed."""
    sessions = {}
    found = []
    stats = {"messages": 0, "sessions": 0}
    with _open(path) as handle:
        for line in handle:
            head = line[:400]
            if '"kind": "session"' in head or '"kind":"session"' in head:
                record = json.loads(line)
                if record.get("kind") == "session":
                    sessions[str(record["id"])] = (record.get("payload") or {}).get("source")
                    stats["sessions"] += 1
                    continue
            if '"kind": "message"' in head or '"kind":"message"' in head:
                stats["messages"] += 1
            if "image_url" not in line and "input_image" not in line and '"type": "image"' not in line:
                continue
            record = json.loads(line)
            if record.get("kind") != "message":
                continue
            found.extend(candidates_of(record))
    for c in found:
        sid = c.source_ref["session_id"]
        c.channel = channel_for(sid, sessions.get(sid), automation_sources)
        c.context["label"] = "Hermes" if c.channel == MESSAGES else "Hermes automation"
    return found, stats


def discover(conn, settings, *, limit=None):
    path = settings.hermes_export
    if not path or not path.exists():
        return
    found, _ = scan(path, settings.hermes_automation_sources)
    if conn is not None and found:
        known = set()
        for channel in (MESSAGES, AUTOMATION):
            ids = sorted({c.external_id for c in found if c.channel == channel})
            if ids:
                known |= {
                    (channel, r["external_id"])
                    for r in conn.execute(
                        "SELECT m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id"
                        " WHERE c.name=%s AND m.superseded_by IS NULL AND m.external_id = ANY(%s)",
                        (channel, ids),
                    )
                }
        for c in found:
            c.message_known = (c.channel, c.external_id) in known
    for n, c in enumerate(found):
        if limit and n >= limit:
            return
        yield c


REEXPORT = (
    "python fetch.py export --out tl-hermes-images.jsonl  # on the Hermes host, with HERMES_MAX_TEXT=50000000 "
    "(and HERMES_MAX_MESSAGES=0); then set IMAGES_HERMES_EXPORT to the copied file"
)


class Resolver:
    def __init__(self, settings):
        self.settings = settings

    def close(self):
        pass

    def fetch(self, c):
        uri = getattr(c, "uri", None)
        if not uri:
            return Fetched("truncated" if c.skip == "truncated" else "not_found", detail=c.skip)
        try:
            data, mime = sniff.decode_data_uri(uri)
        except sniff.DataUriError as error:
            return Fetched("truncated" if error.reason == "truncated" else "failed", detail=error.reason)
        return Fetched("ok", data, mime)
