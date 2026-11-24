"""The write contract for agents and plugins.

Every rule an agent once had to remember about writing becomes a constraint
or a receipt, so no agent needs to read back what it wrote:

- Idempotency keys. A write may carry an `Idempotency-Key` header (8-200
  characters). Keys are scoped to the token and kept for 30 days in
  tl.write_receipts with the request hash and the response skeleton
  (receipts, ids, counts and statuses; never record text). The same key with
  the same request replays the stored response (receipt.replayed true) with
  no other effect; the same key with a different request is HTTP 409.
  THROUGHLINE_REQUIRE_IDEMPOTENCY=true makes the key mandatory for agent
  tokens on record writes.
- Receipts. receipt(conn, kind, id) reads the stored row back in the same
  transaction: version, status, stored_hash (tl.stored_hash) and counts.
- Compare-and-set. check_hash(conn, kind, id, expected) refuses a change to a
  row whose stored_hash is not the one the writer last saw (HTTP 409 with the
  current hash).

Pinned interface for route authors (other packages import it lazily):

    with writes.idempotent(conn, token, request, body) as slot:
        if slot.replayed:
            return slot.response
        ... do the work ...
        return slot.store(200, response)

    writes.header_key(request) -> str | None
    writes.receipt(conn, kind, id, extra=None) -> dict
    writes.register_receipt(kind, fn)   # fn(conn, id) -> dict or None
"""

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

HEADER = "Idempotency-Key"
KEY_MIN, KEY_MAX = 8, 200
# Expired receipt rows deleted with each newly stored key.
PRUNE_BATCH = 100

# Kinds tl.stored_hash knows, with the record_id prefix of their record view.
KINDS = {
    "message": "m",
    "link": "l",
    "entity": "e",
    "annotation": "a",
    "proposal": "q",
    "provenance": "prov",
    "person": "person",
    "identifier": "identifier",
    "table_def": "table_def",
    "table_row": "t",
    "rule": "rule",
}

# String values a stored response keeps: identifiers, statuses and reasons,
# never record text (content, statements, values, evidence, names).
SAFE_STRINGS = frozenset(
    {
        "kind",
        "status",
        "reason",
        "code",
        "record_id",
        "stored_hash",
        "written_at",
        "derived_kind",
        "op",
        "outcome",
    }
)

# Keys whose whole value is record content: dropped from a stored response
# whatever its type, so neither their numbers nor their nested keys (entity
# metadata, link evidence, proposal payloads, annotation values) are kept.
OPAQUE = frozenset(
    {
        "metadata",
        "evidence",
        "payload",
        "value",
        "fields",
        "content",
        "images",
        "note",
        "statement",
        "attributes",
        "subject",
        "author",
        "location",
        "thread",
        "data",
        "prompt",
        "context_query",
        "worker_config",
        "action_config",
        "owner_correction_note",
    }
)

_RECEIPTS = {}


def register_receipt(kind, fn):
    """Add a receipt kind. fn(conn, id) returns a dict with any of version,
    status, stored_hash, counts, record_id and written_at, or None when the
    row does not exist."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("receipt kind must be a name")
    _RECEIPTS[kind] = fn


def header_key(request):
    """The request's Idempotency-Key, or None. A key outside 8-200 characters
    is a 400."""
    if request is None:
        return None
    value = request.headers.get(HEADER)
    if value is None:
        return None
    value = value.strip()
    if not KEY_MIN <= len(value) <= KEY_MAX:
        raise HTTPException(400, f"{HEADER} must be {KEY_MIN} to {KEY_MAX} characters")
    return value


def required():
    """Whether THROUGHLINE_REQUIRE_IDEMPOTENCY makes keys mandatory for
    agent tokens."""
    return (os.getenv("THROUGHLINE_REQUIRE_IDEMPOTENCY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _canonical(value):
    return json.dumps(
        jsonable_encoder(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def request_hash(method, path, body):
    """sha256 over the method, path and canonical JSON of the body."""
    if hasattr(body, "model_dump"):
        body = body.model_dump(mode="json")
    return hashlib.sha256(
        _canonical({"method": method, "path": path, "body": body}).encode()
    ).hexdigest()


def skeleton(value, key=None):
    """What a stored response keeps: dicts and lists, numbers, booleans and
    nulls, and strings only under SAFE_STRINGS keys (or in lists of them).
    Values under OPAQUE keys are dropped whole."""
    if key in OPAQUE:
        return None
    if isinstance(value, dict):
        return {k: skeleton(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [skeleton(v, key) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat() if key in SAFE_STRINGS or key == "written_at" else None
    if isinstance(value, str) and key in SAFE_STRINGS:
        return value
    return None


def _replayed(value):
    """Mark every receipt in a stored response as replayed."""
    if isinstance(value, dict):
        out = {k: _replayed(v) for k, v in value.items()}
        if "stored_hash" in out and "replayed" in out:
            out["replayed"] = True
        return out
    if isinstance(value, list):
        return [_replayed(v) for v in value]
    return value


class Slot:
    """The idempotency slot of one write request."""

    def __init__(self, conn, token, key, method, path, digest):
        self.conn = conn
        self.token = token
        self.key = key
        self.method = method
        self.path = path
        self.request_hash = digest
        self.replayed = False
        self.response = None
        self.status_code = 200

    def store(self, status_code, response):
        """Keep the response skeleton under the key (no-op without a key) and
        return the response unchanged."""
        if self.key is None or self.replayed:
            return response
        kept = skeleton(jsonable_encoder(response))
        self.conn.execute(
            "DELETE FROM tl.write_receipts WHERE token_id=%s AND idempotency_key=%s",
            (self.token["id"], self.key),
        )
        # Expired keys are never replayed; prune a bounded batch of them with
        # each new key so the table does not grow without limit.
        self.conn.execute(
            "DELETE FROM tl.write_receipts r USING (SELECT token_id,idempotency_key FROM tl.write_receipts WHERE expires_at<=now() ORDER BY expires_at LIMIT %s) old WHERE r.token_id=old.token_id AND r.idempotency_key=old.idempotency_key",
            (PRUNE_BATCH,),
        )
        self.conn.execute(
            "INSERT INTO tl.write_receipts(token_id,idempotency_key,method,path,request_hash,status_code,response) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                self.token["id"],
                self.key,
                self.method,
                self.path,
                self.request_hash,
                status_code,
                Jsonb(kept),
            ),
        )
        self.status_code = status_code
        return response


@contextmanager
def idempotent(conn, token, request, body):
    """Call first inside the route's own connection and transaction.

    Same key and request hash: slot.replayed is true and slot.response is the
    stored response (receipts marked replayed); the route returns it with no
    other effect. Same key, different request: 409. No key or a new key: the
    route does its work and calls slot.store(status_code, response) before
    commit. Keys are scoped to the token.
    """
    key = header_key(request)
    method = request.method if request is not None else "POST"
    path = request.url.path if request is not None else ""
    if key is None and token.get("kind") == "agent" and required():
        raise HTTPException(
            400, f"{HEADER} is required for agent record writes on this server"
        )
    digest = request_hash(method, path, body)
    slot = Slot(conn, token, key, method, path, digest)
    if key is not None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"idempotency:{token['id']}:{key}",),
        )
        row = conn.execute(
            "SELECT request_hash,status_code,response FROM tl.write_receipts WHERE token_id=%s AND idempotency_key=%s AND expires_at>now()",
            (token["id"], key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != digest:
                raise HTTPException(
                    409, "idempotency key reused with a different request"
                )
            slot.replayed = True
            slot.status_code = row["status_code"]
            slot.response = _replayed(row["response"])
    yield slot


def _counts(conn, kind, ident):
    if kind in ("message", "entity", "person", "table_row"):
        links = conn.execute(
            "SELECT count(*) FILTER (WHERE from_kind=%(k)s AND from_id=%(i)s) AS f,count(*) FILTER (WHERE to_kind=%(k)s AND to_id=%(i)s) AS t FROM tl.links WHERE status='active' AND ((from_kind=%(k)s AND from_id=%(i)s) OR (to_kind=%(k)s AND to_id=%(i)s))",
            {"k": kind, "i": ident},
        ).fetchone()
    else:
        links = {"f": 0, "t": 0}
    citing = 0
    if kind == "message":
        citing = conn.execute(
            "SELECT count(*) AS n FROM tl.provenance WHERE (derived_kind='message' AND derived_id=%(i)s) OR source_message_id=%(i)s",
            {"i": ident},
        ).fetchone()["n"]
    elif kind in ("passage", "link", "entity", "annotation", "table_row"):
        citing = conn.execute(
            "SELECT count(*) AS n FROM tl.provenance WHERE derived_kind=%s AND derived_id=%s",
            (kind, ident),
        ).fetchone()["n"]
    passages = timeline = sources = 0
    if kind == "message":
        passages = conn.execute(
            "SELECT count(*) AS n FROM tl.passages WHERE message_id=%s", (ident,)
        ).fetchone()["n"]
        # Derived records linked 'about' to this record (a page's timeline).
        timeline = conn.execute(
            "SELECT count(*) AS n FROM tl.links l JOIN tl.messages d ON d.id=l.from_id JOIN tl.channels c ON c.id=d.channel_id WHERE l.status='active' AND l.type='about' AND l.from_kind='message' AND l.to_kind='message' AND l.to_id=%s AND c.record_kind='derived'",
            (ident,),
        ).fetchone()["n"]
    return dict(
        links_from=links["f"],
        links_to=links["t"],
        provenance_citing=citing,
        passages=passages,
        timeline=timeline,
        sources=sources,
    )


_STATE = {
    "message": "SELECT version,CASE WHEN superseded_by IS NULL THEN 'current' ELSE 'superseded' END AS status,ingested_at AS written_at FROM tl.messages WHERE id=%s",
    "link": "SELECT status,created_at AS written_at FROM tl.links WHERE id=%s",
    "entity": "SELECT CASE WHEN merged_into IS NULL THEN 'active' ELSE 'merged' END AS status,now() AS written_at FROM tl.entities WHERE id=%s",
    "annotation": "SELECT status,created_at AS written_at FROM tl.annotations WHERE id=%s",
    "proposal": "SELECT status,created_at AS written_at FROM tl.proposals WHERE id=%s",
    "person": "SELECT coalesce(status,'active') AS status,created_at AS written_at FROM tl.people WHERE id=%s",
    "identifier": "SELECT coalesce(status,'confirmed') AS status,updated_at AS written_at FROM tl.identifiers WHERE id=%s",
    "table_def": "SELECT version,CASE WHEN enabled THEN 'enabled' ELSE 'disabled' END AS status,updated_at AS written_at FROM tl.table_defs WHERE id=%s",
    "table_row": "SELECT version,status,updated_at AS written_at FROM tl.table_rows WHERE id=%s",
    "rule": "SELECT CASE WHEN enabled THEN 'enabled' ELSE 'disabled' END AS status,updated_at AS written_at FROM tl.rules WHERE id=%s",
}


def stored_hash(conn, kind, ident, derived_kind=None):
    """tl.stored_hash of a row (None when it does not exist)."""
    name = kind
    if kind == "provenance" and derived_kind and derived_kind != "message":
        name = "provenance:" + derived_kind
    return conn.execute("SELECT tl.stored_hash(%s,%s) AS h", (name, ident)).fetchone()[
        "h"
    ]


def receipt(conn, kind, id, extra=None):
    """Read a written row back in the same transaction.

    {kind, id, record_id, version, status, stored_hash, counts {links_from,
    links_to, provenance_citing, passages, timeline, sources}, written_at,
    replayed: false} plus `extra`. version is the message version; for other
    kinds it is the row id. Raises LookupError when the row does not exist.
    For kind 'provenance', id is the derived record and extra may name its
    derived_kind (default message).
    """
    extra = dict(extra or {})
    if kind in _RECEIPTS:
        found = _RECEIPTS[kind](conn, id)
        if found is None:
            raise LookupError(f"{kind} {id} not found")
        base = dict(
            kind=kind,
            id=id,
            record_id=f"{kind}:{id}",
            version=id,
            status=None,
            stored_hash=None,
            counts=_zero_counts(),
            written_at=None,
            replayed=False,
        )
        base.update(found)
        base.update(extra)
        base["replayed"] = False
        return jsonable_encoder(base)
    if kind not in KINDS:
        raise ValueError(f"unknown receipt kind: {kind}")
    if kind == "provenance":
        derived_kind = extra.get("derived_kind") or "message"
        digest = stored_hash(conn, kind, id, derived_kind)
        if digest is None:
            raise LookupError(f"no provenance for {derived_kind} {id}")
        state = conn.execute(
            "SELECT count(*) AS n,max(created_at) AS written_at FROM tl.provenance WHERE derived_kind=%s AND derived_id=%s",
            (derived_kind, id),
        ).fetchone()
        result = dict(
            kind=kind,
            id=id,
            record_id=f"prov:{derived_kind}:{id}",
            version=id,
            status="recorded",
            stored_hash=digest,
            counts=dict(_zero_counts(), sources=state["n"]),
            written_at=state["written_at"],
            replayed=False,
            derived_kind=derived_kind,
        )
        result.update(extra)
        result["replayed"] = False
        return jsonable_encoder(result)
    state = conn.execute(_STATE[kind], (id,)).fetchone()
    if state is None:
        raise LookupError(f"{kind} {id} not found")
    result = dict(
        kind=kind,
        id=id,
        record_id=f"{KINDS[kind]}:{id}",
        version=state.get("version", id),
        status=state["status"],
        stored_hash=stored_hash(conn, kind, id),
        counts=_counts(conn, kind, id),
        written_at=state["written_at"],
        replayed=False,
    )
    result.update(extra)
    result["replayed"] = False
    return jsonable_encoder(result)


def row_receipt(kind, ident, row, extra=None):
    """A receipt for a registry row without a tl.stored_hash kind (link
    types, identity imports): the hash covers the row as read back."""
    digest = hashlib.sha256(_canonical(row).encode()).hexdigest()
    result = dict(
        kind=kind,
        id=ident,
        record_id=f"{kind}:{ident}",
        version=ident,
        status=(row or {}).get("status") if isinstance(row, dict) else None,
        stored_hash=digest,
        counts=_zero_counts(),
        written_at=None,
        replayed=False,
    )
    result.update(extra or {})
    return jsonable_encoder(result)


def _zero_counts():
    return dict(
        links_from=0,
        links_to=0,
        provenance_citing=0,
        passages=0,
        timeline=0,
        sources=0,
    )


def expected_hash(data):
    """Pop and validate expected_hash from a request body (None if absent)."""
    if not isinstance(data, dict) or "expected_hash" not in data:
        return None
    value = data.pop("expected_hash")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("expected_hash must be a receipt's stored_hash")
    return value


def check_hash(conn, kind, ident, expected, derived_kind=None):
    """Compare-and-set: refuse with 409 (and the current hash) when the stored
    row no longer has the hash the writer saw. No-op when expected is None.
    Call after locking the row."""
    if expected is None:
        return
    current = stored_hash(conn, kind, ident, derived_kind)
    if current != expected:
        raise HTTPException(
            409,
            {
                "reason": "hash_conflict",
                "message": f"{kind} {ident} changed since that receipt",
                "current_hash": current,
            },
        )
