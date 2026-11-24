"""The query helper of the web interface (docs/web/assistant.md).

    POST /v1/web/assistant                 start a run -> 202 {run, status, provider, model, tier}
    GET  /v1/web/assistant/{run}           follow it (long poll: after, wait 0..25 s)
    POST /v1/web/assistant/{run}/cancel    stop it

Owner and agent tokens (a restricted asker too: every tool reads under the
request's scope through the reader path of POST /v1/sql); plugin tokens 403.
A run belongs to the token and asker that started it: anyone else gets 404.
Plain request/response routes; following a run is a long poll, not a stream.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ...assistant import runs
from ..deps import Token, audit, require, scope_of

router = APIRouter()

HISTORY_BYTES = 8 * 1024


class ErrorShape(BaseModel):
    code: str | None = Field(default=None, max_length=10)
    message: str | None = Field(default=None, max_length=2000)


class ColumnShape(BaseModel):
    name: str = Field(max_length=200)
    type: str | None = Field(default=None, max_length=64)


class ResultShape(BaseModel):
    # The shape of the editor's last result: never its rows.
    columns: list[ColumnShape] = Field(default_factory=list, max_length=400)
    row_count: int | None = Field(default=None, ge=0)
    truncated: bool | None = None


class TimeScope(BaseModel):
    from_: str | None = Field(default=None, alias="from", max_length=64)
    to: str | None = Field(default=None, max_length=64)
    tz: str | None = Field(default=None, max_length=64)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=HISTORY_BYTES)


class AssistantRequest(BaseModel):
    verb: Literal["ask", "fix", "explain", "refine"]
    question: str | None = Field(default=None, max_length=4000)
    statement: str | None = Field(default=None, max_length=100000)
    selection: str | None = Field(default=None, max_length=20000)
    error: ErrorShape | None = None
    result: ResultShape | None = None
    tier: Literal["schema", "samples"] | None = None
    scope: TimeScope | None = None
    channel: str | None = Field(default=None, max_length=200)
    history: list[Turn] = Field(default_factory=list, max_length=10)

    @field_validator("history")
    @classmethod
    def _history_size(cls, value):
        if sum(len(t.text.encode("utf-8")) for t in value) > HISTORY_BYTES:
            raise ValueError("history is larger than 8 KB")
        return value

    @model_validator(mode="after")
    def _needs(self):
        def has(value):
            return isinstance(value, str) and value.strip() != ""

        if self.verb == "ask" and not has(self.question):
            raise ValueError("ask needs a question")
        if self.verb == "refine" and not (has(self.question) and has(self.statement)):
            raise ValueError("refine needs a question and a statement")
        if self.verb == "fix" and not has(self.statement):
            raise ValueError("fix needs a statement")
        if self.verb == "explain" and not (has(self.statement) or has(self.selection)):
            raise ValueError("explain needs a statement or a selection")
        return self

    def plain(self):
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.scope is not None:
            data["scope"] = self.scope.model_dump(by_alias=True, exclude_none=True)
        data["history"] = [t.model_dump() for t in self.history]
        return data


def _started(run):
    return dict(
        run=run.id,
        status=run.status,
        provider=run.provider.name,
        model=run.provider.model,
        tier=run.tier,
    )


@router.post("/v1/web/assistant", status_code=202)
def assistant_start(data: AssistantRequest, token: Token):
    """Start a run of the query helper; follow it with GET."""
    require(token, "owner", "agent")
    try:
        run = runs.start(token, scope_of(token), data.plain())
    except runs.TooManyRuns as exc:
        audit(token, refused="busy" if isinstance(exc, runs.Busy) else "too_many_runs")
        return JSONResponse(
            {"detail": exc.detail},
            status_code=429,
            headers={"Retry-After": "5"},
        )
    audit(token, run=run.id, verb=data.verb, tier=run.tier)
    return _started(run)


@router.get("/v1/web/assistant/{run_id}")
async def assistant_follow(
    run_id: str,
    token: Token,
    after: int = Query(default=0, ge=0),
    wait: float = Query(default=0, ge=0, le=25),
):
    """A run's events after `after`; waits up to `wait` seconds for one."""
    require(token, "owner", "agent")
    run = runs.lookup(token, run_id)
    if run is None:
        raise HTTPException(404, "no such run (runs live in memory for 30 minutes)")
    await run.wait(after, wait)
    view = run.view(after)
    audit(token, run=run.id, events=len(view["events"]))
    return view


@router.post("/v1/web/assistant/{run_id}/cancel")
def assistant_cancel(run_id: str, token: Token):
    """Stop a run; a run that already ended keeps its status."""
    require(token, "owner", "agent")
    run = runs.lookup(token, run_id)
    if run is None:
        raise HTTPException(404, "no such run (runs live in memory for 30 minutes)")
    runs.cancel(run)
    audit(token, run=run.id)
    return {"status": run.status}
