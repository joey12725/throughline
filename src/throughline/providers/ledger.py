"""tl.model_ledger: one row per provider call made outside the embedding worker.

The worker keeps its own tl.embedding_ledger (one row per batch); the view
tl.spend is both together.
"""

from ..db import connect

ORIGINS = ("query", "worker", "job", "rule", "curator", "librarian", "assistant")


def record(
    *,
    purpose,
    provider,
    model,
    tokens=0,
    cost=0,
    calls=1,
    token_id=None,
    trace_id=None,
    origin="query",
    origin_ref=None,
    conn=None,
):
    """Write one ledger row; returns its id."""
    if origin not in ORIGINS:
        raise ValueError("ledger origin must be one of " + ", ".join(ORIGINS))
    statement = (
        "INSERT INTO tl.model_ledger(purpose,provider,model,tokens,cost,calls,token_id,trace_id,origin,origin_ref)"
        # A token deleted while its request was running leaves an unattributed
        # row instead of failing the request it paid for.
        " VALUES (%s,%s,%s,%s,%s,%s,(SELECT id FROM tl.tokens WHERE id=%s),%s,%s,%s) RETURNING id"
    )
    values = (
        purpose,
        provider,
        model,
        int(tokens or 0),
        cost or 0,
        int(calls or 0),
        token_id,
        str(trace_id) if trace_id is not None else None,
        origin,
        None if origin_ref is None else str(origin_ref),
    )
    if conn is not None:
        return conn.execute(statement, values).fetchone()["id"]
    with connect("app") as own:
        return own.execute(statement, values).fetchone()["id"]


def spend(conn, since=None, until=None):
    """Totals over tl.spend (model calls and embedding batches) for a period."""
    where, values = [], []
    if since is not None:
        where.append("at>=%s")
        values.append(since)
    if until is not None:
        where.append("at<%s")
        values.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT coalesce(sum(cost),0) cost,coalesce(sum(tokens),0) tokens,coalesce(sum(calls),0) calls FROM tl.spend"
        + clause,
        values,
    ).fetchone()
    rows = conn.execute(
        "SELECT purpose,provider,model,origin,sum(cost) cost,sum(tokens) tokens,sum(calls) calls,min(at) first_at,max(at) last_at FROM tl.spend"
        + clause
        + " GROUP BY purpose,provider,model,origin ORDER BY sum(cost) DESC,purpose,provider,model,origin",
        values,
    ).fetchall()
    return dict(
        cost=total["cost"], tokens=total["tokens"], calls=total["calls"], by=rows
    )
