"""Concept links and proposals (BRN-12, LIB-6).

Concept links use the app role and call require_unrestricted first: a
restricted scope gets 403 before any lookup and writes only proposals. Plugin
tokens are refused. Proposals are the caller's own working state: any owner
or agent token may propose under any scope (every referenced message is
checked under that scope; a hidden and an unknown id are the same 404) and
lists its own; the owner decides.

Every write takes an Idempotency-Key and answers with a readback receipt
(throughline.writes).
"""

from fastapi import APIRouter, HTTPException, Request

from ... import proposals, writes
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    is_unrestricted,
    own_working_state,
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


def _body(data):
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    return data


@router.post("/v1/concepts/link")
def concept_link(data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner", "agent")
    body = _body(data)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _run(
                proposals.link_concept,
                conn,
                token,
                body.get("target"),
                body.get("entity"),
                {
                    k: body[k]
                    for k in ("job", "job_version", "confidence", "method")
                    if k in body
                },
            )
            audit(token, link_id=row["id"])
            return slot.store(
                200, dict(row, receipt=writes.receipt(conn, "link", row["id"]))
            )


@router.post("/v1/proposals")
@own_working_state
def proposal_create(data: dict, token: Token, request: Request):
    require(token, "owner", "agent")
    body = _body(data)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _run(
                proposals.propose,
                conn,
                scope_of(token),
                token,
                body.get("kind"),
                body.get("payload"),
                body.get("evidence"),
                body.get("origin") or "agent",
                body.get("investigation_id"),
            )
            audit(token, proposal_id=row["id"], kind=row["kind"])
            return slot.store(
                200, dict(row, receipt=writes.receipt(conn, "proposal", row["id"]))
            )


@router.get("/v1/proposals")
@own_working_state
def proposal_list(token: Token, status: str | None = None, limit: int = 100):
    require(token, "owner", "agent")
    unrestricted = is_unrestricted(token)
    with authenticated_connection(token) as conn:
        rows = proposals.list_proposals(conn, token, unrestricted, status, limit)
        audit(token, len(rows))
        return rows


@router.post("/v1/proposals/{proposal_id}/decide")
def proposal_decide(proposal_id: int, data: dict, token: Token, request: Request):
    require_unrestricted(token)
    require(token, "owner")
    body = _body(data)
    if not isinstance(body.get("accept"), bool):
        raise ValueError("accept must be true or false")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _run(
                proposals.decide,
                conn,
                token,
                proposal_id,
                body["accept"],
                body.get("note"),
            )
            audit(token, proposal_id=proposal_id, status=row["status"])
            return slot.store(
                200, dict(row, receipt=writes.receipt(conn, "proposal", proposal_id))
            )
