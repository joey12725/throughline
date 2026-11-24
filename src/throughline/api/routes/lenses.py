"""HTTP routes for lenses, jobs, and arrival rules (M4: TBL-1..5, JOB-1..7, RUL-1..3, ACL-4, ACL-6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ... import lenses, writes
from ...lenses import LensSpecError
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LensSpecError as exc:
        raise HTTPException(422, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/v1/lenses")
def lens_define(data: dict, token: Token, request: Request):
    """Create or update an lens definition (owner and agent tokens; ACL-6)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(lenses.define_lens, data, token=token, conn=conn)
            receipt = writes.receipt(conn, "table_def", row["id"])
            audit(token, 1, lens=row["name"], version=row["version"])
            return slot.store(200, {"lens": row, "receipt": receipt})


@router.get("/v1/lenses")
def lens_list(token: Token):
    """List lens definitions (unrestricted owner/agent only)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        rows = lenses.list_lenses(conn=conn)
    audit(token, len(rows))
    return {"lenses": rows}


@router.get("/v1/lenses/{name}")
def lens_show(name: str, token: Token):
    """Show one lens definition and cursor state."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        row = _handle(lenses.get_lens, name, conn=conn)
    audit(token, 1, lens=row["name"])
    return {"lens": row}


@router.delete("/v1/lenses/{name}")
def lens_delete(name: str, token: Token, request: Request):
    """Delete an lens definition and drop its generated views."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name}) as slot:
            if slot.replayed:
                return slot.response
            res = _handle(lenses.delete_lens, name, conn=conn)
            audit(token, 1, lens=name)
            return slot.store(200, res)


@router.post("/v1/lenses/{name}/quote")
def lens_quote(name: str, data: dict, token: Token):
    """Estimate items, tokens, and USD cost before running a lens (JOB-6)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    res = _handle(
        lenses.quote_lens,
        name,
        backfill=bool(data.get("backfill", False)),
        limit=data.get("limit"),
        since=data.get("since"),
        until=data.get("until"),
        message_ids=data.get("message_ids"),
    )
    audit(token, res.get("items", 0), lens=name)
    return res


@router.post("/v1/lenses/{name}/run")
def lens_run(name: str, data: dict, token: Token, request: Request):
    """Run one lens (owner and agent tokens; ACL-6)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name, **data}) as slot:
            if slot.replayed:
                return slot.response
            res = _handle(
                lenses.run_lens,
                name,
                trigger_kind=str(data.get("trigger_kind") or "manual"),
                backfill=bool(data.get("backfill", False)),
                dry_run=bool(data.get("dry_run", False)),
                limit=data.get("limit"),
                since=data.get("since"),
                until=data.get("until"),
                message_ids=data.get("message_ids"),
                max_cost=data.get("max_cost"),
            )
            audit(token, res.get("written", 0), lens=name, status=res.get("status"))
            return slot.store(200, res)


@router.post("/v1/lenses/run-batch")
def lens_run_batch(data: dict, token: Token, request: Request):
    """Run multiple lenses in dependency order with single-pass LLM merging (JOB-4, JOB-5)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            res = _handle(
                lenses.run_batch,
                names=data.get("names"),
                limit=data.get("limit"),
            )
            audit(token, len(res.get("lenses") or []))
            return slot.store(200, res)


@router.post("/v1/lenses/tick")
def lens_tick(data: dict, token: Token, request: Request):
    """Run due lenses and resume the ones an outage interrupted (lenses.scheduler)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            res = _handle(
                lenses.tick,
                names=data.get("names"),
                dry_run=bool(data.get("dry_run", False)),
                rules=bool(data.get("rules", True)),
            )
            audit(token, res.get("ran", 0))
            return slot.store(200, res)


@router.get("/v1/lenses/{name}/rows")
def lens_rows_read(
    name: str,
    token: Token,
    limit: int = 100,
    versions: bool = False,
):
    """Read rows from api.lens_<name> through the gated reader path (ACL-4)."""
    require(token, "owner", "agent")
    res = _handle(
        lenses.query_lens_rows,
        name,
        scope=scope_of(token),
        limit=limit,
        include_versions=versions,
    )
    audit(token, res["row_count"], lens=name)
    return res


@router.patch("/v1/lenses/{name}/rows/{row_id}")
def lens_row_edit(name: str, row_id: int, data: dict, token: Token, request: Request):
    """Owner hand-correction of a lens row: locks against future overwrites and stores feedback (TBL-4)."""
    require_unrestricted(token)
    require(token, "owner")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name, "row_id": row_id, **data}) as slot:
            if slot.replayed:
                return slot.response
            expected = data.get("expected_hash")
            if expected is not None:
                writes.check_hash(conn, "table_row", row_id, expected)
            row = _handle(
                lenses.edit_row,
                name,
                row_id,
                data.get("data") or {},
                note=data.get("note"),
                lock=bool(data.get("lock", True)),
                status=data.get("status"),
                conn=conn,
            )
            receipt = writes.receipt(conn, "table_row", row["id"])
            audit(token, 1, lens=name, row_id=row["id"])
            return slot.store(200, {"row": row, "receipt": receipt})


@router.post("/v1/rules")
def rule_define(data: dict, token: Token, request: Request):
    """Create or update an arrival rule (owner and agent tokens; ACL-6, RUL-1)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(lenses.define_rule, data, conn=conn)
            receipt = writes.receipt(conn, "rule", row["id"])
            audit(token, 1, rule=row["name"])
            return slot.store(200, {"rule": row, "receipt": receipt})


@router.get("/v1/rules")
def rule_list(token: Token):
    """List defined arrival rules."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        rows = lenses.list_rules(conn=conn)
    audit(token, len(rows))
    return {"rules": rows}


@router.post("/v1/rules/evaluate")
def rule_evaluate(data: dict, token: Token, request: Request):
    """Evaluate enabled arrival rules against new messages (RUL-1..3)."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            res = _handle(
                lenses.evaluate_rules,
                rule_name=data.get("name"),
                message_ids=data.get("message_ids"),
                limit=int(data.get("limit", 200)),
            )
            audit(token, res.get("matched", 0))
            return slot.store(200, res)
