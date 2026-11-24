"""Annotations over HTTP.

POST /v1/annotations: an owner token with an unrestricted scope writes
directly; an agent token (any scope) or a restricted scope gets 202 with a
proposal id (every referenced message checked under that scope); plugin
tokens get 403. Supersede and retract are owner-only. Reading annotations and
effective values runs through the audited reader path, where the asker gate
applies (speaker annotations are unrestricted-only).

Writes take an Idempotency-Key and answer with a readback receipt
(throughline.writes); supersede and retract accept expected_hash (a
receipt's stored_hash) and refuse a changed annotation with 409.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from psycopg import sql

from ... import annotations as annotation_store  # the handler `annotations` below shadows the bare name
from ... import lineage, writes
from ..deps import (
    Token,
    audit,
    audited,
    authenticated_connection,
    is_unrestricted,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()


def _run(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as error:
        raise HTTPException(403, str(error) or "forbidden") from None
    except LookupError as error:
        raise HTTPException(404, str(error) or "not found") from None


def _target(data):
    if isinstance(data.get("target"), dict):
        return data["target"]
    if "message_id" in data:
        return {"message_id": data["message_id"]}
    return {"channel": data.get("channel"), "external_id": data.get("external_id")}


@router.post("/v1/annotations")
def annotation_create(data: dict, token: Token, response: Response, request: Request):
    require(token, "owner", "agent")
    direct = token["kind"] == "owner" and is_unrestricted(token)
    if direct:
        require_unrestricted(token)
    args = (
        _target(data),
        data.get("attribute"),
        data.get("value"),
        data.get("reason"),
        {k: data[k] for k in lineage.VALIDITY if k in data},
        data.get("target_version"),
        data.get("confidence"),
    )
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                response.status_code = slot.status_code
                return slot.response
            result = _run(
                annotation_store.create,
                conn,
                token,
                *args,
                scope=scope_of(token),
                restricted=not direct,
            )
            if "proposal" in result:
                response.status_code = 202
                audit(token, proposal_id=result["proposal"]["id"])
                return slot.store(
                    202,
                    {
                        "proposal_id": result["proposal"]["id"],
                        "proposal": result["proposal"],
                        "receipt": writes.receipt(
                            conn, "proposal", result["proposal"]["id"]
                        ),
                    },
                )
            audit(token, annotation_id=result["annotation"]["id"])
            return slot.store(
                200,
                dict(
                    result,
                    receipt=writes.receipt(
                        conn, "annotation", result["annotation"]["id"]
                    ),
                ),
            )


def _cas(conn, annotation_id, expected):
    """Compare-and-set: lock the annotation and compare its stored hash."""
    if expected is None:
        return
    if not conn.execute(
        "SELECT 1 FROM tl.annotations WHERE id=%s FOR UPDATE", (annotation_id,)
    ).fetchone():
        raise HTTPException(404, "annotation not found")
    writes.check_hash(conn, "annotation", annotation_id, expected)


@router.post("/v1/annotations/{annotation_id}/supersede")
def annotation_supersede(
    annotation_id: int, data: dict, token: Token, request: Request
):
    require_unrestricted(token)
    require(token, "owner")
    body = dict(data)
    expected = writes.expected_hash(body)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            _cas(conn, annotation_id, expected)
            result = _run(
                annotation_store.supersede,
                conn,
                token,
                annotation_id,
                body.get("value"),
                body.get("reason"),
            )
            audit(
                token,
                annotation_id=annotation_id,
                superseded_by=result["annotation"]["id"],
            )
            return slot.store(
                200,
                dict(
                    result,
                    receipt=writes.receipt(
                        conn, "annotation", result["annotation"]["id"]
                    ),
                ),
            )


@router.post("/v1/annotations/{annotation_id}/retract")
def annotation_retract(annotation_id: int, data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner")
    body = dict(data)
    expected = writes.expected_hash(body)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be text")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            _cas(conn, annotation_id, expected)
            row = _run(annotation_store.retract, conn, token, annotation_id, reason)
            audit(token, annotation_id=annotation_id)
            return slot.store(
                200,
                dict(row, receipt=writes.receipt(conn, "annotation", annotation_id)),
            )


@router.get("/v1/annotations")
def annotations(message_id: int, token: Token):
    statement = (
        sql.SQL(
            "SELECT a.* FROM api.annotations a JOIN api.message_versions m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id WHERE m.id={} ORDER BY a.id"
        )
        .format(sql.Literal(message_id))
        .as_string()
    )
    return audited(statement, token)


@router.get("/v1/effective")
def effective(ids: str, token: Token):
    try:
        values = [int(part) for part in ids.split(",") if part.strip()]
    except ValueError:
        raise ValueError("ids must be comma-separated message ids") from None
    if not values or len(values) > 1000:
        raise ValueError("ids must name 1 to 1000 message ids")
    statement = (
        sql.SQL("SELECT * FROM api.effective(ARRAY[{}]::bigint[])")
        .format(sql.SQL(",").join(sql.Literal(v) for v in values))
        .as_string()
    )
    return audited(statement, token)
