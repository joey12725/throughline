"""The link-type registry over HTTP (LNK-1, LNK-2).

GET /v1/link-types reads api.link_types through the audited reader path
(restricted askers see active types only, without counts). with_counts=true
and status=candidate|retired are owner-side reads (require_unrestricted):
counts over every link, candidates and retired_but_written. Register, merge
and retire are owner-only.
"""

from fastapi import APIRouter, HTTPException, Request

from ... import vocabulary, writes
from ..deps import (
    Token,
    audit,
    audited,
    authenticated_connection,
    require,
    require_unrestricted,
)

router = APIRouter()


def _run(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as error:
        raise HTTPException(403, str(error) or "forbidden") from None
    except LookupError as error:
        raise HTTPException(404, str(error) or "not found") from None


@router.get("/v1/link-types")
def link_types(token: Token, with_counts: bool = False, status: str | None = None):
    if not with_counts and status is None:
        return audited("SELECT * FROM api.link_types ORDER BY type", token)
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        rows = vocabulary.list_types(conn, status, with_counts)
        audit(token, len(rows))
        return {"rows": rows, "row_count": len(rows)}


def _registry_receipt(conn, name):
    row = conn.execute(
        "SELECT type,description,inverse,status,origin,plugin FROM tl.link_types WHERE type=%s",
        (name,),
    ).fetchone()
    return writes.row_receipt("link_type", name, row) if row else None


@router.post("/v1/link-types")
def link_type_register(data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _run(
                vocabulary.register,
                conn,
                token,
                data.get("type"),
                data.get("description"),
                data.get("inverse"),
            )
            audit(token, type=row["type"])
            return slot.store(
                200, dict(row, receipt=_registry_receipt(conn, row["type"]))
            )


@router.post("/v1/link-types/{link_type}/merge")
def link_type_merge(link_type: str, data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner")
    if not isinstance(data.get("into"), str):
        raise ValueError("into must name a link type")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            result = _run(vocabulary.merge, conn, token, link_type, data["into"])
            audit(token, **result)
            return slot.store(
                200, dict(result, receipt=_registry_receipt(conn, result["into"]))
            )


@router.post("/v1/link-types/{link_type}/retire")
def link_type_retire(link_type: str, data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner")
    into = data.get("into")
    if into is not None and not isinstance(into, str):
        raise ValueError("into must name a link type")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            result = _run(vocabulary.retire, conn, token, link_type, into)
            audit(token, **result)
            return slot.store(
                200,
                dict(
                    result,
                    receipt=_registry_receipt(conn, result.get("type") or link_type),
                ),
            )
