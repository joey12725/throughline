"""Local, explicitly registered parsers: no network or plugin imports."""

import os
from datetime import timedelta, timezone

from .ingest import ingest
from .models import Ingest, Message

MAPPERS = {}


def register_mapper(channel, mapper):
    MAPPERS[channel] = mapper


def batch_size():
    """Records re-mapped per transaction (THROUGHLINE_REPARSE_BATCH, default
    500). Each batch commits on its own, so a large reparse never holds one
    ingest transaction open past the feed settle window (migration 0046)."""
    try:
        value = int(os.getenv("THROUGHLINE_REPARSE_BATCH") or 500)
    except ValueError:
        value = 500
    return max(1, min(value, 5000))


def _commit(conn):
    """Commit the current batch and start the next transaction with the same
    token attribution (a transaction-local setting)."""
    token_id = conn.execute(
        "SELECT current_setting('throughline.token_id',true) AS t"
    ).fetchone()["t"]
    conn.commit()
    if token_id:
        conn.execute("SELECT set_config('throughline.token_id',%s,true)", (token_id,))


def reparse(conn, channel):
    if channel not in MAPPERS:
        raise ValueError("no local mapper registered for channel")
    c = conn.execute("SELECT * FROM tl.channels WHERE name=%s", (channel,)).fetchone()
    if not c:
        raise ValueError("channel not found")
    rows = conn.execute(
        "SELECT m.*,t.external_id thread_external_id,t.title,p.payload FROM tl.messages m JOIN tl.raw_payloads p ON p.message_id=m.id JOIN tl.threads t ON t.id=m.thread_id WHERE m.channel_id=%s AND m.superseded_by IS NULL ORDER BY m.id",
        (c["id"],),
    ).fetchall()
    result = dict(received=0, inserted=0, new_versions=0, unchanged=0, rejected=[])
    size = batch_size()
    # Reading the rows is its own transaction; then every batch of records is
    # mapped, ingested and committed in one transaction (same mapping and the
    # same per-item results as one large call).
    _commit(conn)
    for start in range(0, len(rows), size):
        _reparse_batch(conn, channel, c, rows[start : start + size], result)
        _commit(conn)
    return result


def _reparse_batch(conn, channel, c, rows, result):
    messages = []
    skipped = 0
    for r in rows:
        mapped = MAPPERS[channel](r["payload"])
        message = dict(
            channel=channel,
            external_id=r["external_id"],
            thread=dict(external_id=r["thread_external_id"], title=r["title"]),
            happened_at=r["happened_at"]
            .astimezone(timezone(timedelta(minutes=r["happened_offset_min"] or 0)))
            .isoformat(),
            ended_at=r["ended_at"].isoformat() if r["ended_at"] else None,
            content=r["content"],
            fields=r["fields"],
            images=r["images"],
            access_label=r["access_label"],
            payload=r["payload"],
        )
        if r["author_identifier_id"]:
            a = conn.execute(
                "SELECT * FROM tl.identifiers WHERE id=%s", (r["author_identifier_id"],)
            ).fetchone()
            message["author"] = {k: a[k] for k in ("system", "kind", "value")}
        if r["loc_method"] == "source":
            message["location"] = dict(
                lat=r["lat"], lon=r["lon"], accuracy_m=r["loc_accuracy_m"]
            )
        if any(
            r[k] is not None
            for k in (
                "subject_from",
                "subject_to",
                "subject_from_precision",
                "subject_to_precision",
            )
        ):
            message["subject"] = dict(
                start=r["subject_from"].isoformat() if r["subject_from"] else None,
                end=r["subject_to"].isoformat() if r["subject_to"] else None,
                start_precision=r["subject_from_precision"] or "exact",
                end_precision=r["subject_to_precision"] or "exact",
            )
        envelope = conn.execute(
            "SELECT envelope FROM tl.message_envelopes WHERE message_id=%s", (r["id"],)
        ).fetchone()
        if envelope:
            message.update(envelope["envelope"])
        desired = dict(message)
        desired.update(mapped)
        # Compare only the requested transformation before rebuilding versions.
        # Legacy rows may lack source-only envelope fields; a no-op must not
        # manufacture a version just because those inputs cannot be recovered.
        if Message.model_validate(desired).model_dump(
            mode="json"
        ) == Message.model_validate(message).model_dump(mode="json"):
            skipped += 1
            continue
        messages.append(desired)
    result["received"] += skipped
    result["unchanged"] += skipped
    if messages:
        part = ingest(conn, Ingest(plugin=c["plugin"], messages=messages))
        for key in ("received", "inserted", "new_versions", "unchanged"):
            result[key] += part[key]
        result["rejected"].extend(part["rejected"])
