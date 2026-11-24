"""POST /v1/librarian and GET /v1/investigations/{id} (QRY-39, QRY-44, LIB-9, ACL-10).

POST runs one bounded investigation under the token's scope and returns its
EvidencePacket. The asker comes from the token only; an 'asker' in the body is
ignored. Owner and agent tokens may call it; a restricted asker gets exactly
what its scope allows.

GET returns an investigation to the token and asker that ran it, or to the
owner: request, plans, executed operations (from the trace), failed avenues,
coverage, reason and the packet rebuilt from the stored result set and notes
under the caller's current scope (members hidden or purged since, and the
notes that cite them, drop out). Nothing else is stored. After an access
policy change it is 409; another caller's id is 404, like an unknown one.
"""

import uuid
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import Field

from ... import cost, packets, resultsets
from ...librarian import RecursionRefused, investigate
from ...librarian.request import LibrarianRequest
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    own_working_state,
    require,
    scope_of,
)

router = APIRouter()


class LibrarianBody(LibrarianRequest):
    approval_id: str | None = Field(default=None, max_length=64)


@contextmanager
def _errors():
    try:
        yield
    except resultsets.ResultSetNotFound as error:
        raise HTTPException(404, str(error) or "result set not found") from None
    except resultsets.ResultSetStale as error:
        raise HTTPException(409, str(error)) from None
    except PermissionError as error:
        raise HTTPException(403, str(error)) from None
    except RecursionRefused as error:
        raise HTTPException(400, str(error)) from None
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


@router.post("/v1/librarian")
def librarian(data: LibrarianBody, token: Token):
    """Investigate a goal with the optional librarian; see api.describe('librarian')."""
    require(token, "owner", "agent")
    request = LibrarianRequest.model_validate(
        data.model_dump(exclude={"approval_id", "asker"})
    )
    with _errors():
        packet = investigate(
            scope_of(token), token, request, approval_id=data.approval_id
        )
    audit(
        token,
        len(packet["records"]),
        operation="librarian",
        investigation_id=packet["investigation_id"],
        status=packet["execution_status"],
        trace_id=(packet.get("investigation") or {}).get("trace_id"),
    )
    return jsonable_encoder(packet)


def _members(conn, handle):
    """Member references (ids only) of a stored set that has not expired."""
    return conn.execute(
        "SELECT m.record_id,m.message_id,m.version,m.span_first,m.span_last,m.score,m.role,m.depends_on,m.discovery,m.meta"
        " FROM tl.result_set_members m JOIN tl.result_sets s ON s.handle=m.handle"
        " WHERE m.handle=%s AND s.expires_at>now() ORDER BY m.ordinal",
        (handle,),
    ).fetchall()


def rebuild(scope, row, refs=None):
    """The packet of a stored investigation under the caller's scope now.
    refs: member references when the caller does not own the stored set."""
    handle = row["result_handle"]
    if not handle:
        return None, "the investigation delivered no result set"
    notes = list(row["notes"] or [])
    unresolved = list((row["coverage"] or {}).get("unresolved_needs") or [])
    if refs is not None:
        if not refs:
            return None, "the investigation's result set has expired"
        handle = resultsets.create(
            scope,
            [
                dict(
                    record_id=r["record_id"],
                    message_id=r["message_id"],
                    version=r["version"],
                    span_first=r["span_first"],
                    span_last=r["span_last"],
                    score=r["score"],
                    role=r["role"],
                    depends_on=r["depends_on"],
                    discovery=r["discovery"] or [],
                    meta=r["meta"] or {},
                )
                for r in refs
            ],
            completeness="truncated",
            origin=dict(op="investigation", parents=[row["result_handle"]]),
        )
    try:
        packet = packets.build(
            scope,
            handle,
            notes=notes,
            unresolved=unresolved,
            investigation=str(row["id"]),
        )
    except resultsets.ResultSetNotFound:
        return None, "the investigation's result set has expired"
    packet["execution_status"] = row["status"]
    packet["incompleteness_reason"] = row["reason"]
    packet["continuation_ref"] = dict(
        investigation_id=str(row["id"]), result_set=handle, offset=None
    )
    return packet, None


@router.get("/v1/investigations/{investigation_id}")
@own_working_state
def investigation(investigation_id: str, token: Token):
    """An investigation of yours, rebuilt under your current scope."""
    require(token, "owner", "agent")
    try:
        investigation_id = str(uuid.UUID(investigation_id))
    except ValueError:
        raise HTTPException(404, "investigation not found") from None
    audit(token, investigation_id=investigation_id)
    scope = scope_of(token)
    with authenticated_connection(token) as conn:
        row = conn.execute(
            "SELECT * FROM tl.investigations WHERE id=%s", (investigation_id,)
        ).fetchone()
        mine = bool(row) and (row["token_id"], row["asker"]) == (
            token["id"],
            token.get("asker") or None,
        )
        # The owner reads any investigation; an owner token acting for an
        # asker is that asker.
        is_owner = token["kind"] == "owner" and scope.mode == "owner"
        if not row or not (mine or is_owner):
            raise HTTPException(404, "investigation not found")
        if int(row["policy_version"]) != int(cost.policy_version(conn) or 0):
            raise HTTPException(409, "access policy changed since this investigation")
        events = []
        if row["trace_id"]:
            events = conn.execute(
                "SELECT seq,op,statement,status,rows,ms,model,detail FROM tl.trace_events WHERE trace_id=%s ORDER BY seq",
                (row["trace_id"],),
            ).fetchall()
        own_set = (row["token_id"], row["asker"]) == (
            scope.token_id,
            scope.asker or None,
        )
        refs = None
        if not own_set and row["result_handle"]:
            refs = _members(conn, row["result_handle"])
    with _errors():
        packet, unavailable = rebuild(scope, row, refs)
    audit(token, len(packet["records"]) if packet else 0)
    return jsonable_encoder(
        dict(
            id=str(row["id"]),
            status=row["status"],
            reason=row["reason"],
            policy_version=row["policy_version"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            request=row["request"],
            plans=row["plans"],
            executed_operations=[
                dict(
                    seq=e["seq"],
                    op=e["op"],
                    statement=e["statement"],
                    status=e["status"],
                    rows=e["rows"],
                    ms=e["ms"],
                    model=e["model"],
                    detail=e["detail"],
                )
                for e in events
            ],
            failed_avenues=row["failed"],
            coverage=row["coverage"],
            counts=row["counts"],
            baseline_handle=row["baseline_handle"],
            result_handle=row["result_handle"],
            packet=packet,
            packet_unavailable=unavailable,
        )
    )
