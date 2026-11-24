"""POST /v1/plans and POST /v1/packets (QRY-41, QRY-42, LIB-5, LIB-9).

Both run on the reader path under the token's scope (the asker gate applies
in the database) and write only the caller's own result sets, so a restricted
asker may call them. A plan over the model-cost limit answers 200 with
execution.status 'approval_required' and runs nothing. Unknown or foreign
handles are 404, a set from an earlier access policy is 409.
"""

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from ... import packets as packet_library
from ... import plans as plan_library
from ... import resultsets
from ...budget import Budget, clamp
from ...retrieval.types import Budgets
from ..deps import Token, audit, require, scope_of

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


class PlanRequest(BaseModel):
    plan: dict[str, Any]
    budgets: Budgets | None = None
    dry_run: bool = False
    approval_id: str | None = Field(default=None, max_length=64)


class NoteRequest(BaseModel):
    generated_text: str = Field(max_length=packet_library.MAX_NOTE_CHARS)
    supporting_refs: list[dict[str, Any] | str] = Field(default_factory=list)
    uncertainty: str | float | None = None


class PacketRequest(BaseModel):
    handle: str
    notes: list[NoteRequest] = Field(
        default_factory=list, max_length=packet_library.MAX_NOTES
    )
    unresolved: list[str] = Field(default_factory=list, max_length=100)
    token_budget: int | None = Field(default=None, ge=1)


@router.post("/v1/plans")
def run_plan(data: PlanRequest, token: Token):
    """Validate and run a plan of sql, recall, expand, match, transform and
    inspect nodes under one budget; see api.describe('plans')."""
    require(token, "owner", "agent")
    with _errors():
        result = plan_library.execute(
            scope_of(token),
            token,
            data.plan,
            budgets=data.budgets.model_dump() if data.budgets is not None else None,
            dry_run=data.dry_run,
            approval_id=data.approval_id,
        )
    execution = result["execution"]
    audit(
        token,
        len(result["handles"]),
        operation="plan",
        nodes=len(data.plan.get("nodes") or []),
        status=execution.get("status"),
        **({"trace_id": execution["trace_id"]} if execution.get("trace_id") else {}),
    )
    return jsonable_encoder(result)


@router.post("/v1/packets")
def build_packet(data: PacketRequest, token: Token):
    """An evidence packet of one of your result sets: records copied from a
    live read, notes kept only with verified references."""
    require(token, "owner", "agent")
    budget = None
    if data.token_budget is not None:
        budget = clamp(Budget(tokens=data.token_budget), token).tokens
    with _errors():
        packet = packet_library.build(
            scope_of(token),
            data.handle,
            notes=[n.model_dump() for n in data.notes],
            unresolved=list(data.unresolved),
            token_budget=budget,
        )
    audit(
        token,
        len(packet["records"]),
        operation="packet",
        result_set=data.handle,
        notes_kept=len(packet["orientation"]),
        omissions=len(packet["truncation_and_omissions"]),
    )
    return jsonable_encoder(packet)
