"""Read back a trace (QRY-36, QRY-44).

A trace is the caller's own working state: statements, ids, counts and hashes
of what it ran itself, never excerpts. The owner reads any trace. An agent
reads only traces written by the same token for the same asker (anything else
is 404, so ids of other callers' traces are not confirmed), and only while the
access policy is the one the trace was written under (otherwise 409).
"""

import uuid

from fastapi import APIRouter, HTTPException

from ..deps import Token, audit, authenticated_connection, own_working_state, require

router = APIRouter()


@router.get("/v1/traces/{trace_id}")
@own_working_state
def trace_get(trace_id: str, token: Token):
    require(token, "owner", "agent")
    try:
        trace_id = str(uuid.UUID(trace_id))
    except ValueError:
        raise HTTPException(404, "trace not found") from None
    audit(token, trace_id=trace_id)
    with authenticated_connection(token) as conn:
        trace = conn.execute(
            "SELECT * FROM tl.traces WHERE id=%s", (trace_id,)
        ).fetchone()
        if not trace or (
            token["kind"] != "owner"
            and (
                trace["token_id"] != token["id"] or trace["asker"] != token.get("asker")
            )
        ):
            raise HTTPException(404, "trace not found")
        if token["kind"] != "owner":
            current = 0
            if conn.execute(
                "SELECT to_regprocedure('api.policy_version()') IS NOT NULL ok"
            ).fetchone()["ok"]:
                current = conn.execute("SELECT api.policy_version() v").fetchone()["v"]
            if (current or 0) != trace["policy_version"]:
                raise HTTPException(409, "access policy changed since this trace")
        events = conn.execute(
            "SELECT seq,op,statement,status,rows,ms,model,tokens,cost,detail FROM tl.trace_events WHERE trace_id=%s ORDER BY seq",
            (trace_id,),
        ).fetchall()
        audit(token, len(events))
        trace["events"] = events
        return trace
