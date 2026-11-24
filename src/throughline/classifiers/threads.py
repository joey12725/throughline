"""Load a thread as classifier input: its current messages in order, each
with its participant key, and the participants.

The participant of a message is tl.participant_key(effective person,
author identifier, author handle): an owner 'speaker' annotation that assigns
a transcript turn to a person moves that turn to the person's participant.
"""

from datetime import datetime

THREAD_SQL = """
SELECT m.id,m.channel_id,m.external_id,m.happened_at,m.content,m.fields,m.author_handle,
 coalesce(sp.person_id,m.person_id) AS person_id,
 tl.participant_key(coalesce(sp.person_id,m.person_id),
   CASE WHEN sp.person_id IS NULL THEN m.author_identifier_id END,m.author_handle) AS participant,
 p.display_name,p.handle AS person_handle,coalesce(p.is_owner,false) AS is_owner,
 i.kind AS identifier_kind,i.label AS identifier_label
FROM tl.messages m
LEFT JOIN LATERAL (SELECT (a.value->>'person_id')::bigint AS person_id FROM tl.annotations a
  WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id
   AND a.attribute='speaker' AND a.status='active'
  ORDER BY a.method='owner' DESC,a.id DESC LIMIT 1) sp ON true
LEFT JOIN tl.people p ON p.id=coalesce(sp.person_id,m.person_id)
LEFT JOIN tl.identifiers i ON i.id=m.author_identifier_id
WHERE m.thread_id=%s AND m.superseded_by IS NULL
ORDER BY m.thread_no NULLS LAST,m.happened_at,m.id
"""


def _label(row):
    if row["display_name"]:
        return row["display_name"]
    if row["person_handle"]:
        return row["person_handle"]
    fields = row["fields"] or {}
    if isinstance(fields.get("speaker_label"), str) and fields["speaker_label"].strip():
        return fields["speaker_label"].strip()
    if row["identifier_label"]:
        return row["identifier_label"]
    return row["author_handle"] or "unknown"


def _offset(row, first_at):
    fields = row["fields"] or {}
    start = fields.get("start_s")
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        return float(start)
    if isinstance(row["happened_at"], datetime) and isinstance(first_at, datetime):
        return max(0.0, (row["happened_at"] - first_at).total_seconds())
    return None


def load(conn, thread_id, *, drop_ids=()):
    """(thread data for the runtime, rows by message id). drop_ids: messages
    left out (for example text that may not leave this machine)."""
    head = conn.execute(
        "SELECT t.id,t.title,c.name AS channel FROM tl.threads t JOIN tl.channels c ON c.id=t.channel_id WHERE t.id=%s",
        (thread_id,),
    ).fetchone()
    if not head:
        raise LookupError("thread not found")
    drop = set(drop_ids)
    rows = [dict(r) for r in conn.execute(THREAD_SQL, (thread_id,)) if r["id"] not in drop]
    first_at = rows[0]["happened_at"] if rows else None
    participants = {}
    messages = []
    for r in rows:
        key = r["participant"]
        if key not in participants:
            participants[key] = {
                "key": key,
                "label": _label(r),
                "is_owner": bool(r["is_owner"]),
                "person_id": r["person_id"],
            }
        messages.append(
            {
                "id": r["id"],
                "at": r["happened_at"].isoformat() if isinstance(r["happened_at"], datetime) else r["happened_at"],
                "offset_s": _offset(r, first_at),
                "participant": key,
                "text": r["content"] or "",
            }
        )
    # Labels must tell participants apart.
    seen = {}
    for p in participants.values():
        seen.setdefault(p["label"], []).append(p)
    for label, group in seen.items():
        if len(group) > 1:
            for n, p in enumerate(group, 1):
                p["label"] = f"{label} #{n}"
    ends = [m["offset_s"] for m in messages if m["offset_s"] is not None]
    last = rows[-1] if rows else None
    end_s = None
    if last:
        f = last["fields"] or {}
        end_s = f.get("end_s") if isinstance(f.get("end_s"), (int, float)) else None
    data = {
        "id": head["id"],
        "channel": head["channel"],
        "title": head["title"],
        "started_at": first_at.isoformat() if isinstance(first_at, datetime) else None,
        "duration_s": float(end_s if end_s is not None else (max(ends) if ends else 0.0)),
        "participants": list(participants.values()),
        "messages": messages,
    }
    return data, {r["id"]: r for r in rows}


def owner_name(conn):
    row = conn.execute(
        "SELECT coalesce(display_name,handle) AS name FROM tl.people WHERE is_owner AND merged_into IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return row["name"] if row else None
