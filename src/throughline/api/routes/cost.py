"""Cost approvals and spend (CST-10, CST-3).

An approval is the caller's own working state: an estimate (purposes, models,
counts, cost) for a request it made itself, never text. Listing therefore does
not need an unrestricted scope and shows an agent only what its own token and
asker asked for. Deciding and reading spend are refused to a restricted asker
before any lookup.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from ... import cost
from ...providers import ledger
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    is_unrestricted,
    own_working_state,
    require,
    require_unrestricted,
)

router = APIRouter()


@router.get("/v1/approvals")
@own_working_state
def approvals(token: Token, status: str | None = None):
    require(token, "owner", "agent")
    if status is not None and status not in (
        "pending",
        "approved",
        "used",
        "rejected",
    ):
        raise ValueError("status must be pending, approved, used or rejected")
    # The owner, asking as itself, sees every approval; anyone else its own.
    everything = token["kind"] == "owner" and is_unrestricted(token)
    with authenticated_connection(token) as conn:
        rows = cost.list_approvals(
            conn,
            token_id=token["id"],
            asker=token.get("asker"),
            own=not everything,
            status=status,
        )
    audit(token, len(rows))
    return rows


def _decide(approval_id, token, status):
    require_unrestricted(token)
    require(token, "owner", "agent")
    agent = token["kind"] != "owner"
    audit(token, approval_id=approval_id, decision=status)
    with authenticated_connection(token) as conn:
        try:
            return cost.decide(
                conn,
                approval_id,
                status,
                by=f"{token['kind']} token {token['id']}",
                token_id=token["id"],
                asker=token.get("asker"),
                own=agent,
                limit=token.get("approve_limit") or 0 if agent else None,
            )
        except LookupError:
            raise HTTPException(404, "approval not found") from None
        except PermissionError as error:
            raise HTTPException(403, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None


@router.post("/v1/approvals/{approval_id}/approve")
def approve(approval_id: str, token: Token):
    """Owner: any pending approval. Agent: its own, up to its approve_limit."""
    return _decide(approval_id, token, "approved")


@router.post("/v1/approvals/{approval_id}/reject")
def reject(approval_id: str, token: Token):
    return _decide(approval_id, token, "rejected")


@router.get("/v1/spend")
def spend(token: Token, since: datetime | None = None, until: datetime | None = None):
    require_unrestricted(token)
    require(token, "owner")
    with authenticated_connection(token) as conn:
        result = ledger.spend(conn, since, until)
    audit(token, len(result["by"]))
    return result
