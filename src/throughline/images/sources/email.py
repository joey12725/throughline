"""Email image attachments (channel email.message), personal mailboxes only.

Taken: image parts (by mime type or extension) of messages whose sender
address is at a personal-mail domain and whose local part is not an
automated one; parts under 15 KB are skipped here, under 100 px a side after
the bytes arrive (signature and tracking images).

Bytes: Gmail attachments are fetched on the mail host (Composio keys stay
there) by images/email_fetch.py, which reads a request list written by
`throughline lib images email-requests` and writes <dir>/index.jsonl plus the
files. IMAGES_EMAIL_IMPORT_DIR points at that directory once it is copied here.
"""

import json
import re
from pathlib import Path

from .. import sniff
from . import Candidate, Fetched

CHANNEL = "email.message"
PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "aol.com", "hotmail.com", "outlook.com", "live.com",
        "msn.com", "yahoo.com", "icloud.com", "me.com", "mac.com", "proton.me",
        "protonmail.com", "pm.me",
    }
)
# hotmail.co.uk, yahoo.co.jp, live.ca ...: the provider label decides.
PERSONAL_LABELS = frozenset({"hotmail", "outlook", "live", "msn", "yahoo", "aol", "gmail", "googlemail"})
AUTOMATED_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply[-_.].*|.*[-_.]noreply|mailer[-_.]daemon|postmaster|"
    r"bounces?|notifications?|notify|alerts?|newsletter|news|info|support|billing|receipts?|hello|team|marketing)$",
    re.I,
)
MIN_BYTES = 15 * 1024
MIN_SIDE = 100


def personal_sender(address):
    """True for a person's own mailbox at a consumer mail provider."""
    if not address or "@" not in address:
        return False
    local, domain = address.strip().lower().rsplit("@", 1)
    if AUTOMATED_LOCAL.match(local):
        return False
    if domain in PERSONAL_DOMAINS:
        return True
    labels = domain.split(".")
    if labels[0] not in PERSONAL_LABELS:
        return False
    # Country variants only: hotmail.fr, yahoo.co.uk, outlook.com.au.
    if len(labels) == 2:
        return len(labels[1]) == 2
    return len(labels) == 3 and labels[1] in ("co", "com") and len(labels[2]) == 2


QUERY = """
SELECT m.external_id, m.happened_at, m.fields, p.display_name AS person, t.title AS thread_title
  FROM tl.messages m
  JOIN tl.channels c ON c.id = m.channel_id
  LEFT JOIN tl.threads t ON t.id = m.thread_id
  LEFT JOIN tl.people p ON p.id = m.person_id
 WHERE c.name = 'email.message' AND m.superseded_by IS NULL
   AND jsonb_typeof(m.fields->'attachments') = 'array'
   AND jsonb_array_length(m.fields->'attachments') > 0
 ORDER BY m.happened_at DESC
"""


def candidates_of(row, imported=None):
    fields = row["fields"] or {}
    sender = fields.get("from_address")
    personal = personal_sender(sender)
    context = {
        "label": "Email",
        "thread": fields.get("subject") or row.get("thread_title"),
        "author": row.get("person") or fields.get("from_name") or sender,
        "body": None,
    }
    out = []
    for i, a in enumerate(fields.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        name, mime = a.get("filename"), a.get("mime_type")
        c = Candidate(
            origin="email",
            channel=CHANNEL,
            external_id=row["external_id"],
            ord=i,
            role="inline" if a.get("inline") else "attachment",
            name=name,
            mime=sniff.mime_of(name, mime),
            size=a.get("size"),
            happened_at=row.get("happened_at"),
            source_ref={
                "account": fields.get("account"),
                "message_id": fields.get("gmail_message_id"),
                "attachment_id": a.get("attachment_id"),
                "content_id": a.get("content_id") or None,
                "file_name": name,
            },
            context=context,
            message_known=True,
        )
        if not sniff.is_image(name, mime):
            c.skip = "not_image"
        elif not personal:
            c.skip = "not_personal_sender"
        elif (a.get("size") or 0) < MIN_BYTES:
            c.skip = "under_15kb"
        elif not a.get("attachment_id") or not fields.get("gmail_message_id"):
            c.skip = "no_attachment_id"
        else:
            c.route = "export"
            if imported is not None and request_key(c) not in imported:
                c.skip = "needs_email_export"
        out.append(c)
    return out


def request_key(c):
    return (c.source_ref.get("message_id"), c.source_ref.get("attachment_id"))


def read_index(directory):
    """{(message_id, attachment_id): index row} of an email_fetch.py output directory."""
    index = {}
    if not directory:
        return index
    path = Path(directory) / "index.jsonl"
    if not path.exists():
        return index
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        index[(row.get("message_id"), row.get("attachment_id"))] = row
    return index


def discover(conn, settings, *, limit=None):
    imported = read_index(settings.email_import_dir)
    count = 0
    for row in conn.execute(QUERY):
        for c in candidates_of(row, imported):
            yield c
            count += 1
            if limit and count >= limit:
                return


def requests(candidates):
    """The request list email_fetch.py reads: one per taken attachment."""
    return [
        {
            "account": c.source_ref["account"],
            "message_id": c.source_ref["message_id"],
            "attachment_id": c.source_ref["attachment_id"],
            "file_name": c.name,
            "mime_type": c.mime,
        }
        for c in candidates
        if c.route == "export" and c.skip in (None, "needs_email_export")
    ]


class Resolver:
    def __init__(self, settings):
        self.dir = settings.email_import_dir
        self.index = read_index(self.dir)

    def close(self):
        pass

    def fetch(self, c):
        row = self.index.get(request_key(c))
        if not row:
            return Fetched("pending_export", detail="not in IMAGES_EMAIL_IMPORT_DIR")
        if row.get("error") or not row.get("file"):
            return Fetched("failed", detail=str(row.get("error") or "no file")[:200])
        try:
            data = (Path(self.dir) / row["file"]).read_bytes()
        except OSError as error:
            return Fetched("not_found", detail=type(error).__name__)
        _, w, h = sniff.sniff(data)
        if w and h and (w < MIN_SIDE or h < MIN_SIDE):
            return Fetched("filtered", detail="under_100px")
        if len(data) < MIN_BYTES:
            return Fetched("filtered", detail="under_15kb")
        return Fetched("ok", data)
