"""Shared request plumbing for every route module.

Authentication, the per-request Scope, the audit entry, the app-role
connection and the audited reader path live here so route modules under
api/routes/ import them without importing the application.

Rule for new routes: a route that uses the app role (authenticated_connection)
must call require_unrestricted(token) before any lookup, unless it serves only
the caller's own working state (its sessions, result sets, proposals,
approvals, traces). Everything a restricted asker may read goes through the
reader path (audited / query_ex with scope_of(token)), where the database gate
applies.
"""

import hashlib
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth import sessions
from ..db import connect
from ..execution import Trace
from ..query import query_ex, reader_transaction
from ..scope import Scope

bearer = HTTPBearer(auto_error=False)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def same_origin_request(request, always=False):
    """Refuse a cookie-authenticated request another site could have made.

    The session cookie is SameSite=Lax, and the API answers no CORS
    preflight, so a page elsewhere cannot add a custom header. A state
    changing request (and, with always=True, any request) must therefore
    carry X-Throughline-Client, and a browser that says the request is
    cross-site or same-site (a sibling host) is refused outright.
    """
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site request refused")
    if (always or request.method not in SAFE_METHODS) and not request.headers.get(
        "x-throughline-client"
    ):
        raise HTTPException(403, "this request needs the X-Throughline-Client header")


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    x_throughline_asker: Annotated[str | None, Header()] = None,
):
    if credentials:
        with connect("app") as conn:
            token = conn.execute(
                "SELECT * FROM tl.tokens WHERE token_hash=%s AND revoked_at IS NULL",
                (hashlib.sha256(credentials.credentials.encode()).hexdigest(),),
            ).fetchone()
        if not token:
            raise HTTPException(401, "invalid or revoked token")
    else:
        # A signed-in browser: the session cookie resolves to its account's
        # principal token, which is then treated exactly like a bearer token.
        secret = sessions.cookie_secret(request)
        if not secret:
            raise HTTPException(401, "bearer token required")
        same_origin_request(request)
        token = sessions.resolve(secret)
        if not token:
            raise HTTPException(401, "session expired or signed out")
    asker = x_throughline_asker or None
    allowed = list(token["askers"] or [])
    if token["kind"] != "owner":
        if asker and asker not in allowed:
            raise HTTPException(403, "asker is not allowed by this token")
        if not asker and allowed:
            # A token issued for named askers always acts as one of them.
            if len(allowed) > 1:
                raise HTTPException(
                    403, "this token must name an asker (X-Throughline-Asker)"
                )
            asker = allowed[0]
    request.state.token = token
    request.state.asker = asker
    token["asker"] = asker
    return token


Token = Annotated[dict, Depends(authenticate)]


def require(token, *kinds):
    if token["kind"] not in kinds:
        raise HTTPException(403, "token cannot perform this operation")


def plugin_access(token, name):
    require(token, "owner", "plugin")
    if token["kind"] == "plugin" and token["plugin"] != name:
        raise HTTPException(403, "plugin scope mismatch")


def audit(token, row_count=None, **detail):
    """Describe the current request for its single tl.audit_log row.

    Only identifiers and counts are recorded, never request bodies.
    """
    entry = token.setdefault("audit", {})
    if row_count is not None:
        entry["row_count"] = row_count
    if detail:
        entry.setdefault("detail", {}).update(detail)


@contextmanager
def authenticated_connection(token):
    with connect("app") as conn:
        conn.execute(
            "SELECT set_config('throughline.token_id',%s,true)", (str(token["id"]),)
        )
        yield conn


def scope_of(token):
    """The request's Scope, built once from the authenticated token."""
    scope = token.get("scope")
    if not isinstance(scope, Scope):
        scope = token["scope"] = Scope.from_token(token)
    return scope


def is_unrestricted(token):
    """Whether this request's scope sees everything; resolved once per request.

    The database decides (api.asker_unrestricted() under the request's reader
    settings). Before that function exists only an owner-mode scope is
    unrestricted.
    """
    if "unrestricted" not in token:
        scope = scope_of(token)
        with reader_transaction(scope) as conn:
            if conn.execute(
                "SELECT to_regprocedure('api.asker_unrestricted()') IS NOT NULL AS ok"
            ).fetchone()["ok"]:
                value = conn.execute(
                    "SELECT api.asker_unrestricted() AS ok"
                ).fetchone()["ok"]
            else:
                value = scope.mode == "owner"
        token["unrestricted"] = value is True
    return token["unrestricted"]


def own_working_state(endpoint):
    """Mark a route that uses the app role for the caller's own working state
    only (its sessions, result sets, proposals, approvals, traces) and so does
    not call require_unrestricted. Every other app-role route must call it; a
    test enumerates the application's routes and enforces this."""
    endpoint.own_working_state = True
    return endpoint


def flush_trace(token, trace, execution=None):
    """Write a multi-statement operation's trace and name it in the request's
    audit detail. Never raises; returns the trace id, or None (with a warning
    on the passed Execution) when it was not written."""
    if trace.flush(execution):
        audit(token, trace_id=trace.id)
        if execution is not None and hasattr(execution, "trace_id"):
            execution.trace_id = trace.id
        return trace.id
    if execution is not None and hasattr(execution, "warn"):
        execution.warn("trace was not written")
    return None


def require_unrestricted(token):
    """Refuse a restricted scope on an app-role route, before any lookup.

    App-role statements read tl tables directly, below the database gate, so a
    restricted asker gets 403 whether or not the thing it names exists.
    """
    if not is_unrestricted(token):
        raise HTTPException(
            403, "this operation is not available to a restricted asker"
        )


def audited(
    statement,
    token,
    cap=1000,
    *,
    params=None,
    deadline_ms=None,
    token_budget=None,
    execution=None,
    materialize=True,
    trace=None,
    budgets=None,
    dry_run=False,
    approval_id=None,
    strict_exact=False,
    columns=None,
):
    """Run one statement on the reader path under the token's scope.

    The statement is recorded for the request's audit row before it runs, so a
    rejected or failed statement is audited too (row_count 0); the middleware
    writes the row with the HTTP status. trace=True writes a trace for this
    single statement and puts its id in the result's execution and in the audit
    detail; a Trace instance (multi-statement operation) only receives the
    event and is flushed by its owner. Returns query_ex's dict.

    The token row, budgets, dry_run, approval_id and strict_exact reach
    query_ex for statements with semantic calls (semantic.run): the cost gate
    and the budget clamp need the token's limits.
    """
    require(token, "owner", "agent")
    token.setdefault("audit", {}).update(statement=statement, row_count=0)
    if params:
        audit(token, param_count=len(params))
    scope = scope_of(token)
    own = Trace(scope, "sql", {"row_cap": cap}) if trace is True else None
    record = own or (trace if isinstance(trace, Trace) else None)
    try:
        result = query_ex(
            statement,
            cap,
            scope=scope,
            params=params,
            deadline_ms=deadline_ms,
            token_budget=token_budget,
            execution=execution,
            materialize=materialize,
            token_row=token,
            budgets=budgets,
            dry_run=dry_run,
            approval_id=approval_id,
            strict_exact=strict_exact,
            trace=record,
            columns=columns,
        )
    except Exception as exc:
        if record:
            record.event(
                "sql",
                statement,
                "failed",
                detail={"code": getattr(exc, "sqlstate", None)},
            )
        if own and own.flush():
            audit(token, trace_id=own.id)
        raise
    token["audit"]["row_count"] = result["row_count"]
    if record:
        stages = result["execution"]["stages"]
        record.event(
            "sql",
            statement,
            "ok",
            rows=result["row_count"],
            ms=stages[-1].get("ms") if stages else None,
            detail={
                "truncation": (result["execution"]["truncation"] or {}).get("reason")
            },
        )
    if own:
        result["execution"]["trace_id"] = own.id
        if own.flush(result["execution"]):
            audit(token, trace_id=own.id)
        else:
            result["execution"]["trace_id"] = None
            result["execution"]["warnings"].append("trace was not written")
    return result
