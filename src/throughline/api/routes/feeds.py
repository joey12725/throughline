"""Feeds over HTTP: named cursors over arrivals (JOB-2, JOB-7).

Primary routes are /v1/feeds...; every /v1/feeds... route (the feature's
former name) stays as a deprecated alias with identical behaviour, hidden
from the OpenAPI document. Receipt kinds (consumer, consumer_cursor,
consumer_settings) and the `consumer` response keys keep their names; a
response that carries `consumer` also carries `feed`.

Feeds are the caller's own working state (its token and asker), so
restricted askers may use them; what news and pending read passes the
database gate on the reader path. A foreign and an unknown name give the
same 404. Settings: GET for owner and agent tokens, PUT for owner tokens
only (a constraint violation is 400).

Write contract: create, drop and PUT settings honour an
Idempotency-Key (a replay returns the stored response with receipt.replayed
true and changes nothing; the same key with another body is 409); a keyed
request is answered with a receipt (kind consumer or consumer_settings), an
un-keyed one keeps the response shape it always had. The key slot lives on
its own app-role connection and holds the key's advisory lock while
throughline.feeds does the work, so concurrent duplicates write once.
POST mark needs no key: it is idempotent by compare-and-set, and its answer
carries a receipt-shaped object {kind consumer_cursor, id, stored_hash of the
cursor, replayed}. replayed is always false there: the answer is recomputed
under compare-and-set, never replayed from a key store, and a repeated mark
returns the identical answer and receipt (same stored_hash and version).
"""

import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder

from ... import coverage, feeds, writes
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    own_working_state,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()
CONSUMER = "SELECT id,name,token_id,asker,scope,seen_to_ingested_at,seen_to_message_id,marks,created_at,updated_at FROM tl.consumers"


def _run(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as error:
        raise HTTPException(403, str(error) or "forbidden") from None
    except LookupError:
        raise HTTPException(404, "feed not found") from None
    except feeds.ConflictError as error:
        raise HTTPException(409, str(error)) from None


def _body(data):
    if not isinstance(data, dict):
        raise HTTPException(422, "body must be an object")
    return data


# --- receipts -------------------------------------------------------------------


def _hash(row):
    text = json.dumps(
        jsonable_encoder(dict(row)), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _consumer_row(conn, scope, name):
    """The caller's feed row (its token and asker), or None."""
    if not isinstance(name, str):
        return None
    return conn.execute(
        CONSUMER
        + " WHERE name=%s AND token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s",
        (name, scope.token_id, scope.asker),
    ).fetchone()


def _consumer_receipt(conn, consumer_id):
    row = conn.execute(CONSUMER + " WHERE id=%s", (consumer_id,)).fetchone()
    if row is None:
        return None
    return dict(
        record_id=f"consumer:{row['id']}",
        version=row["marks"],
        status="active",
        stored_hash=_hash(row),
        written_at=row["updated_at"],
    )


def _cursor_receipt(conn, consumer_id):
    """stored_hash of the cursor: the feed id and its seen_to position."""
    row = conn.execute(CONSUMER + " WHERE id=%s", (consumer_id,)).fetchone()
    if row is None:
        return None
    cursor = dict(
        consumer=row["id"],
        ingested_at=row["seen_to_ingested_at"],
        message_id=row["seen_to_message_id"],
    )
    return dict(
        record_id=f"consumer_cursor:{row['id']}",
        version=row["marks"],
        status="current",
        stored_hash=_hash(cursor),
        written_at=row["updated_at"],
    )


def _settings_receipt(conn, _ident):
    row = conn.execute(
        "SELECT settle_s,margin_s FROM tl.consumer_settings WHERE one"
    ).fetchone()
    if row is None:
        return None
    return dict(
        record_id="consumer_settings:1", status="current", stored_hash=_hash(row)
    )


writes.register_receipt("consumer", _consumer_receipt)
writes.register_receipt("consumer_cursor", _cursor_receipt)
writes.register_receipt("consumer_settings", _settings_receipt)


# --- routes -----------------------------------------------------------------------
# Each handler answers on /v1/feeds... and on the deprecated /v1/consumers...


@router.get("/v1/feeds/settings")
@router.get("/v1/consumers/settings", include_in_schema=False)
@own_working_state
def settings_get(token: Token):
    require(token, "owner", "agent")
    return feeds.read_settings()


@router.put("/v1/feeds/settings")
@router.put("/v1/consumers/settings", include_in_schema=False)
def settings_put(data: dict, token: Token, request: Request):
    """Change the settle window (owner tokens only); answers {settle_s,
    margin_s, receipt}."""
    require(token, "owner")
    require_unrestricted(token)
    body = _body(data)
    current = feeds.read_settings()
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, body) as slot:
            if slot.replayed:
                audit(token, 0, replayed=True)
                return slot.response
            result = _run(
                feeds.settings,
                conn,
                True,
                body.get("settle_s", current["settle_s"]),
                body.get("margin_s", current["margin_s"]),
            )
            audit(token, 1, **result)
            if slot.key is not None:
                result = dict(
                    result, receipt=writes.receipt(conn, "consumer_settings", 1)
                )
            slot.store(200, result)
    return result


@router.post("/v1/feeds")
@router.post("/v1/consumers", include_in_schema=False)
@own_working_state
def consumer_create(data: dict, token: Token, request: Request):
    """Create a feed; answers the feed with its receipt."""
    require(token, "owner", "agent")
    body = _body(data)
    scope = scope_of(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, body) as slot:
            if slot.replayed:
                audit(token, 0, replayed=True)
                return slot.response
            result = _run(
                feeds.create,
                scope,
                body.get("name"),
                body.get("scope"),
                body.get("start", "now"),
            )
            if slot.key is not None:
                row = _consumer_row(conn, scope, result["name"])
                result = dict(
                    result, receipt=writes.receipt(conn, "consumer", row["id"])
                )
            result = jsonable_encoder(result)
            slot.store(200, result)
    audit(token, 1, consumer=result["name"])
    return result


@router.get("/v1/feeds")
@router.get("/v1/consumers", include_in_schema=False)
@own_working_state
def consumer_list(token: Token):
    require(token, "owner", "agent")
    rows = _run(feeds.list_feeds, scope_of(token))
    audit(token, len(rows))
    return dict(feeds=rows, consumers=rows)


@router.get("/v1/feeds/{name}/news")
@router.get("/v1/consumers/{name}/news", include_in_schema=False)
@own_working_state
def consumer_news(name: str, token: Token, max: int | None = None):
    """A page of arrivals in arrival order (not a population), with
    execution.coverage for the feed's channels."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    result = _run(feeds.news, scope, name, max)
    execution = dict(result["execution"])
    try:
        found = feeds.get(scope, name)
        channels = (found["scope"] or {}).get("channels")
        kinds = (found["scope"] or {}).get("kinds") or ["life_event", "derived"]
        rows = coverage.summary(scope, channels, None, None, kinds)["channels"]
        value = coverage.describe(rows, bool(result["rows"]))
        execution["coverage"] = value
        if not value["covered"]:
            execution["warnings"] = list(execution.get("warnings") or []) + [
                "absence_is_not_evidence"
            ]
    except Exception as exc:  # coverage never fails news
        execution["coverage"] = dict(status="unknown", error=type(exc).__name__)
    result["execution"] = execution
    audit(token, result["row_count"], consumer=name)
    return jsonable_encoder(result)


@router.get("/v1/feeds/{name}/pending")
@router.get("/v1/consumers/{name}/pending", include_in_schema=False)
@own_working_state
def consumer_pending(name: str, token: Token):
    require(token, "owner", "agent")
    result = _run(feeds.pending, scope_of(token), name)
    audit(token, 1, consumer=name)
    return jsonable_encoder(result)


@router.post("/v1/feeds/{name}/mark")
@router.post("/v1/consumers/{name}/mark", include_in_schema=False)
@own_working_state
def consumer_mark(name: str, data: dict, token: Token):
    """Move the cursor after the work is done: {to: {ingested_at,
    message_id}, expected?: {ingested_at, message_id}}. Answers {seen_to,
    marked, pending_after, receipt}. No Idempotency-Key is needed: a repeated
    mark is a no-op with the same answer, receipt included."""
    require(token, "owner", "agent")
    body = _body(data)
    scope = scope_of(token)
    # The receipt is read back inside mark's own transaction, right after the
    # compare-and-set, so a concurrent later mark cannot leak into it.
    result = _run(
        feeds.mark,
        scope,
        name,
        body.get("to"),
        body.get("expected"),
        receipt=lambda conn, ident: writes.receipt(conn, "consumer_cursor", ident),
    )
    receipt = result.get("receipt")
    if receipt is not None:
        # The cursor as stored after this call, the same for the mark that
        # moved it and for any repeat (exactly-once marking, same answer).
        receipt = {k: v for k, v in receipt.items() if k != "written_at"}
    result["receipt"] = receipt
    audit(token, 1, consumer=name)
    return jsonable_encoder(result)


@router.delete("/v1/feeds/{name}")
@router.delete("/v1/consumers/{name}", include_in_schema=False)
@own_working_state
def consumer_drop(name: str, token: Token, request: Request):
    """Drop a feed; answers {name, dropped, receipt} (status dropped,
    stored_hash of the row as it was before the drop)."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {}) as slot:
            if slot.replayed:
                audit(token, 0, consumer=name, replayed=True)
                return slot.response
            row = _consumer_row(conn, scope, name)
            result = _run(feeds.drop, scope, name)
            if slot.key is None:
                slot.store(200, result)
                audit(token, 1, consumer=name)
                return result
            result["receipt"] = writes.row_receipt(
                "consumer",
                row["id"],
                row,
                dict(
                    record_id=f"consumer:{row['id']}",
                    status="dropped",
                    stored_hash=_hash(row),
                    version=row["marks"],
                    written_at=None,
                ),
            )
            slot.store(200, result)
    audit(token, 1, consumer=name)
    return result
