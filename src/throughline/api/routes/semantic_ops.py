"""POST /v1/expand and POST /v1/match: the semantic operators over HTTP
(QRY-32, QRY-33, CST-10, INT-10).

Both read only through the reader path under the token's scope (result sets
and the gated record views), so a restricted asker may call them; the sets
they write are the caller's own working state. The response is the one
result envelope {records, result_set, execution}: records are full Records
read back through resultsets.read, the same hydrator recall and sets use.
An estimate above the cost limit is not an error: execution.status is
approval_required with the approval, HTTP 200, and no model was called.
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ... import cost, resultsets
from ...budget import Budget, BudgetTracker, clamp
from ...execution import Execution, Trace
from ...providers import ProviderUnavailable
from ...retrieval import expand as expand_library
from ...retrieval import match as match_library
from ...retrieval.types import Budgets, HardScope
from ..deps import Token, audit, flush_trace, require, scope_of

router = APIRouter()


@contextmanager
def _errors():
    try:
        yield
    except resultsets.ResultSetNotFound as error:
        raise HTTPException(404, str(error) or "result set not found") from None
    except resultsets.ResultSetStale as error:
        raise HTTPException(409, str(error)) from None
    except ProviderUnavailable as error:
        raise HTTPException(400, str(error)) from None
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


class ExpandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str | None = None
    ids: list[str | int] | None = Field(default=None, max_length=10000)
    mode: Literal["context", "thread", "sources", "links", "time", "place", "focus"]
    before: int = Field(default=3, ge=0, le=50)
    after: int = Field(default=3, ge=0, le=50)
    link_type: str | None = Field(default=None, max_length=200)
    depth: int = Field(default=1, ge=1, le=4)
    as_of: datetime | None = None
    known_by: datetime | None = None
    window_s: int = Field(default=0, ge=0)
    meters: float = Field(default=100, gt=0, le=20_000_000)
    focus: str | None = Field(default=None, max_length=4000)
    k: int = Field(default=20, ge=1, le=1000)
    within: HardScope | None = None
    keep_input: bool = True
    role: Literal["context", "dependency"] = "context"
    session: str | None = None
    budgets: Budgets = Field(default_factory=Budgets)
    trace: bool = False


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str
    condition: str = Field(min_length=1, max_length=match_library.MAX_CONDITION_CHARS)
    max_rows: int = Field(ge=1)
    grain: Literal["record", "episode"] = "record"
    approval_id: str | None = None
    dry_run: bool = False
    session: str | None = None
    budgets: Budgets = Field(default_factory=Budgets)
    trace: bool = False


def _tracker(token, budgets):
    return BudgetTracker(clamp(Budget(**budgets.model_dump()), token))


def _envelope(scope, handle, execution, tracker):
    records = []
    if handle is not None:
        read = resultsets.read(scope, handle, token_budget=tracker.budget.tokens)
        records = read["records"]
        truncation = read["execution"].get("truncation")
        if truncation:
            execution.set_truncation(truncation["reason"], truncation["limit"])
    return dict(records=records, result_set=handle, execution=execution.to_dict())


def _trace(token, data, kind, request):
    if not data.trace:
        return None
    return Trace(scope_of(token), kind, request)


def _finish(token, trace, execution):
    if trace is not None:
        flush_trace(token, trace, execution)


@router.post("/v1/expand")
def expand(data: ExpandRequest, token: Token):
    """Expand a result set by a mechanical relation; see api.describe('expand')."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    tracker = _tracker(token, data.budgets)
    trace = _trace(token, data, "expand", dict(mode=data.mode, input=data.input))
    execution = Execution(tracker, trace_id=trace.id if trace is not None else None)
    with _errors():
        handle = expand_library.expand(
            scope,
            handle=data.input,
            ids=data.ids,
            mode=data.mode,
            before=data.before,
            after=data.after,
            link_type=data.link_type,
            depth=data.depth,
            as_of=data.as_of,
            known_by=data.known_by,
            window_s=data.window_s,
            meters=data.meters,
            focus=data.focus,
            k=data.k,
            within=data.within,
            token_budget=tracker.budget.tokens,
            tracker=tracker,
            execution=execution,
            keep_input=data.keep_input,
            role=data.role,
            session=data.session,
        )
        result = _envelope(scope, handle, execution, tracker)
    _finish(token, trace, execution)
    audit(
        token,
        len(result["records"]),
        operation="expand",
        mode=data.mode,
        handle=handle,
        status=result["execution"]["status"],
    )
    return jsonable_encoder(result)


@router.post("/v1/match")
def match(data: MatchRequest, token: Token):
    """Judge every member of a set against a condition; see api.describe('match')."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    tracker = _tracker(token, data.budgets)
    trace = _trace(token, data, "match", dict(input=data.input, grain=data.grain))
    execution = Execution(tracker, trace_id=trace.id if trace is not None else None)
    with _errors():
        if data.dry_run:
            planned = match_library.estimate(
                scope,
                handle=data.input,
                condition=data.condition,
                max_rows=data.max_rows,
                grain=data.grain,
            )
            execution.set("dry_run", True)
            execution.set("estimate", planned["summary"])
            execution.set("counts", planned["counts"])
            audit(token, 0, operation="match", dry_run=True)
            return jsonable_encoder(
                dict(records=[], result_set=None, execution=execution.to_dict())
            )
        try:
            handle = match_library.match(
                scope,
                token,
                handle=data.input,
                condition=data.condition,
                max_rows=data.max_rows,
                grain=data.grain,
                tracker=tracker,
                execution=execution,
                approval_id=data.approval_id,
                trace=trace,
                session=data.session,
            )
        except cost.ApprovalRequired as required:
            execution.require_approval(required.to_dict())
            _finish(token, trace, execution)
            audit(token, 0, operation="match", status="approval_required")
            return jsonable_encoder(
                dict(records=[], result_set=None, execution=execution.to_dict())
            )
        result = _envelope(scope, handle, execution, tracker)
    _finish(token, trace, execution)
    counts = result["execution"].get("counts") or {}
    audit(
        token,
        len(result["records"]),
        operation="match",
        handle=handle,
        applied=counts.get("applied"),
        cached=counts.get("cached"),
        failed=counts.get("failed"),
        status=result["execution"]["status"],
    )
    return jsonable_encoder(result)
