"""Sessions and result sets over HTTP (QRY-34, QRY-35, LIB-5, LIB-9).

A result set is the caller's own working state, so these routes do not call
require_unrestricted: a restricted asker may build and read its own sets. Every
read of member content goes through the gated record views under the
request's scope (throughline.resultsets), and every record id a caller
supplies is checked through those views first: an unknown, hidden or foreign
id, handle or session is the same 404. A set or session written under an
earlier access policy is 409.
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import resultsets
from ...budget import Budget, clamp
from ..deps import Token, audit, own_working_state, require, scope_of

router = APIRouter()


@contextmanager
def _errors():
    try:
        yield
    except resultsets.ResultSetNotFound as error:
        raise HTTPException(404, str(error) or "result set not found") from None
    except resultsets.ResultSetStale as error:
        raise HTTPException(409, str(error)) from None
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


def _budget(token, requested):
    return clamp(Budget(tokens=requested), token).tokens


class SessionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    ttl_s: int | None = Field(default=None, gt=0)


class MemberRequest(BaseModel):
    record_id: str
    version: int | None = None
    span: dict[str, int | None] | None = None
    score: float | None = None
    role: str = "hit"
    depends_on: str | None = None
    groups: list[str | dict[str, Any]] = []
    meta: dict[str, Any] = {}


class SetRequest(BaseModel):
    records: list[MemberRequest] = Field(max_length=resultsets.MAX_MEMBERS)
    session: str | None = None
    origin: dict[str, Any] | None = None
    completeness: Literal["exhaustive", "ranked_sample", "truncated"] = "exhaustive"
    counts: dict[str, float] | None = None
    as_of: datetime | None = None
    known_by: datetime | None = None
    ttl_s: int | None = Field(default=None, gt=0)


class TransformRequest(BaseModel):
    op: str
    handles: list[str] = Field(min_length=1, max_length=20)
    params: dict[str, Any] = {}
    session: str | None = None
    ttl_s: int | None = Field(default=None, gt=0)


class InspectRequest(BaseModel):
    view: str = "summary"
    selector: dict[str, Any] | str | None = None
    token_budget: int = Field(default=1000, gt=0)


@router.post("/v1/sessions")
@own_working_state
def session_open(data: SessionRequest, token: Token):
    require(token, "owner", "agent")
    with _errors():
        result = resultsets.open_session(scope_of(token), data.note, data.ttl_s)
    audit(token, session_id=result["id"])
    return result


@router.get("/v1/sessions/{session_id}")
@own_working_state
def session_get(session_id: str, token: Token):
    require(token, "owner", "agent")
    audit(token, session_id=session_id[:64])
    with _errors():
        result = resultsets.session(scope_of(token), session_id)
    audit(token, len(result["sets"]))
    return result


@router.post("/v1/sets")
@own_working_state
def set_create(data: SetRequest, token: Token):
    require(token, "owner", "agent")
    scope = scope_of(token)
    with _errors():
        if data.session is not None:
            resultsets.session(scope, data.session)
        named = [r.record_id for r in data.records] + [
            r.depends_on for r in data.records if r.depends_on
        ]
        visible = resultsets.resolve(scope, named)
        for record in data.records:
            row = visible.get(record.record_id)
            if row is None or (
                record.version is not None
                and row["version"] is not None
                and row["version"] != record.version
            ):
                raise resultsets.ResultSetNotFound("record not found")
            if record.depends_on and record.depends_on not in visible:
                raise resultsets.ResultSetNotFound("record not found")
        origin = dict(data.origin or {})
        for parent in origin.get("parents") or []:
            resultsets.info(scope, parent)
        origin.setdefault("op", "create")
        records = []
        for record in data.records:
            row = visible[record.record_id]
            item = record.model_dump()
            item["version"] = row["version"]
            item["message_id"] = row["message_id"]
            records.append(item)
        handle = resultsets.create(
            scope,
            records,
            origin=origin,
            completeness=data.completeness,
            counts=data.counts,
            session=data.session,
            as_of=data.as_of,
            known_by=data.known_by,
            ttl_s=data.ttl_s,
        )
        result = resultsets.info(scope, handle)
    audit(token, result["member_count"], handle=handle)
    return result


@router.post("/v1/sets/transform")
@own_working_state
def set_transform(data: TransformRequest, token: Token):
    require(token, "owner", "agent")
    scope = scope_of(token)
    params = dict(data.params)
    reserved = sorted({"scope", "op", "handles"} & set(params))
    if reserved:
        raise HTTPException(400, "transform: reserved parameter " + ", ".join(reserved))
    if data.session is not None:
        params["session"] = data.session
    if data.ttl_s is not None:
        params["ttl_s"] = data.ttl_s
    with _errors():
        handle = resultsets.transform(scope, data.op, data.handles, **params)
        result = resultsets.info(scope, handle)
    audit(token, result["member_count"], handle=handle, op=data.op)
    return result


@router.get("/v1/sets/{handle}")
@own_working_state
def set_read(
    handle: str,
    token: Token,
    limit: int | None = None,
    offset: int = 0,
    token_budget: int | None = None,
):
    require(token, "owner", "agent")
    audit(token, handle=handle[:64])
    with _errors():
        result = resultsets.read(
            scope_of(token),
            handle,
            limit=None if limit is None else max(0, min(limit, 10000)),
            offset=offset,
            token_budget=_budget(token, token_budget),
        )
    audit(token, len(result["records"]))
    return result


@router.get("/v1/sets/{handle}/info")
@own_working_state
def set_info(handle: str, token: Token):
    require(token, "owner", "agent")
    audit(token, handle=handle[:64])
    with _errors():
        return resultsets.info(scope_of(token), handle)


@router.post("/v1/sets/{handle}/inspect")
@own_working_state
def set_inspect(handle: str, data: InspectRequest, token: Token):
    require(token, "owner", "agent")
    audit(token, handle=handle[:64], view=data.view[:32])
    with _errors():
        return resultsets.inspect(
            scope_of(token),
            handle,
            data.view,
            data.selector,
            token_budget=_budget(token, data.token_budget),
        )


@router.delete("/v1/sets/{handle}")
@own_working_state
def set_drop(handle: str, token: Token):
    require(token, "owner", "agent")
    audit(token, handle=handle[:64])
    with _errors():
        return resultsets.drop(scope_of(token), handle)
