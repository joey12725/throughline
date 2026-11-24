"""Cost gate (CST-10): per-token model-cost limit and one-time approvals.

Every operation that may spend on a non-local model collects its estimates and
calls authorize() before the first call:

    digest = cost.request_hash(statement, scope, [(handle, members)], budgets)
    cost.authorize(token_row, estimates, scope=scope, tracker=tracker,
                   caller_limit=budget.model_cost, approval_id=approval_id,
                   request_hash=digest)

Under the limit it returns. Over the limit it raises ApprovalRequired carrying
a pending approval, unless ``approval_id`` names an approval that was approved
for exactly this token, asker, request and policy version; that one is consumed.
"""

import hashlib
import json
import uuid
from dataclasses import replace

from psycopg.types.json import Jsonb

from .db import connect

# An approved request may run when its new estimate is at most this much above
# the approved cost (estimates move a little between the two calls).
APPROVAL_TOLERANCE = 1.2
DEFAULT_MODEL_COST = 0.10
STATUSES = ("pending", "approved", "used", "rejected")


class ApprovalRequired(Exception):
    """The estimated model cost is over the limit and nothing has been spent."""

    def __init__(self, estimate, limit, approval_id, explanation):
        super().__init__(explanation)
        self.estimate = estimate
        self.limit = limit
        self.approval_id = approval_id
        self.explanation = explanation

    def to_dict(self):
        return dict(
            approval_id=self.approval_id,
            estimate=self.estimate,
            limit=self.limit,
            explanation=self.explanation,
        )


def _plain(value):
    for name in ("to_dict", "model_dump"):
        method = getattr(value, name, None)
        if callable(method):
            return method()
    return str(value)


def request_hash(
    statement_or_request, scope, input_handles_with_member_counts, budgets
):
    """Digest of what an approval is for. Every caller of authorize uses it.

    It covers the statement or request body, the token and asker, every input
    handle with its member count (so a result set that grew is a different
    request) and the budgets. The policy version is checked separately.
    """
    inputs = sorted(
        [str(handle), int(count)]
        for handle, count in (
            input_handles_with_member_counts.items()
            if isinstance(input_handles_with_member_counts, dict)
            else input_handles_with_member_counts or ()
        )
    )
    body = json.dumps(
        dict(
            request=statement_or_request,
            token_id=getattr(scope, "token_id", None),
            asker=getattr(scope, "asker", None),
            inputs=inputs,
            budgets=budgets,
        ),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_plain,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def policy_version(conn):
    """api.policy_version(), or 0 before the asker gate exists."""
    if not conn.execute(
        "SELECT to_regprocedure('api.policy_version()') IS NOT NULL ok"
    ).fetchone()["ok"]:
        return 0
    return conn.execute("SELECT api.policy_version() v").fetchone()["v"] or 0


def summarize(estimates):
    """The estimate stored with an approval: totals and per-purpose parts."""
    parts = [e.to_dict() for e in estimates]
    charged = [p for p in parts if not p["local"]]
    return dict(
        cost=round(sum(float(p["cost"] or 0) for p in charged), 6),
        calls=sum(int(p["calls"] or 0) for p in charged),
        tokens=sum(int(p["tokens"] or 0) for p in charged),
        parts=parts,
    )


def explain(summary, limit):
    """Deterministic text; no model writes it."""
    purposes = sorted(
        {
            f"{p['purpose']} ({p['provider']}/{p['model']})"
            for p in summary["parts"]
            if not p["local"]
        }
    )
    return (
        f"Estimated model cost {summary['cost']:.4f} USD for {summary['calls']} call(s)"
        f" and about {summary['tokens']} tokens ({', '.join(purposes)}) is above the"
        f" limit of {limit:.4f} USD for this request. Nothing has been run or spent."
        " An approval allows this exact request to run once, at up to"
        f" {summary['cost'] * APPROVAL_TOLERANCE:.4f} USD."
    )


def _allow(tracker, total):
    """Let an approved request reserve its estimate on the request's tracker."""
    if tracker is None or tracker.budget.model_cost is None:
        return
    spent = tracker.snapshot()["spent"]["model_cost"]
    if tracker.budget.model_cost < spent + total:
        tracker.budget = replace(tracker.budget, model_cost=spent + total)


def authorize(
    token_row,
    estimates,
    *,
    scope,
    tracker=None,
    caller_limit=None,
    approval_id=None,
    request_hash,
):
    """Pass, or raise ApprovalRequired before any model call.

    The limit is the lowest of the caller's own limit, the token's
    max_model_cost and the model cost of the tracker's budget. Local estimates
    cost nothing and never need approval.
    """
    if scope is None:
        raise ValueError("cost.authorize requires a scope")
    if not request_hash:
        raise ValueError("cost.authorize requires a request_hash")
    summary = summarize(list(estimates))
    total = summary["cost"]
    token_limit = (token_row or {}).get("max_model_cost")
    limits = [float(DEFAULT_MODEL_COST if token_limit is None else token_limit)]
    if caller_limit is not None:
        limits.append(max(0.0, float(caller_limit)))
    if tracker is not None and tracker.budget.model_cost is not None:
        limits.append(float(tracker.budget.model_cost))
    limit = min(limits)
    if total <= limit + 1e-12:
        return dict(
            authorized=True, cost=total, limit=limit, approval_id=None, estimate=summary
        )
    token_id = (token_row or {}).get("id", getattr(scope, "token_id", None))
    asker = getattr(scope, "asker", None)
    with connect("app") as conn:
        version = policy_version(conn)
        if approval_id is not None:
            try:
                wanted = str(uuid.UUID(str(approval_id)))
            except ValueError:
                wanted = None
            used = (
                wanted
                and conn.execute(
                    "UPDATE tl.approvals SET status='used' WHERE id=%s AND status='approved'"
                    " AND token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s"
                    " AND request_hash=%s AND policy_version=%s AND %s<=cost*%s RETURNING id",
                    (
                        wanted,
                        token_id,
                        asker,
                        request_hash,
                        version,
                        total,
                        APPROVAL_TOLERANCE,
                    ),
                ).fetchone()
            )
            if used:
                _allow(tracker, total)
                return dict(
                    authorized=True,
                    cost=total,
                    limit=limit,
                    approval_id=str(used["id"]),
                    estimate=summary,
                )
        explanation = explain(summary, limit)
        # Asking again for the same request renews its pending approval
        # instead of adding one per attempt.
        pending = conn.execute(
            "UPDATE tl.approvals SET estimate=%s,cost=%s,limit_cost=%s,explanation=%s"
            " WHERE id=(SELECT id FROM tl.approvals WHERE status='pending' AND token_id IS NOT DISTINCT FROM %s"
            " AND asker IS NOT DISTINCT FROM %s AND request_hash=%s AND policy_version=%s ORDER BY created_at LIMIT 1 FOR UPDATE)"
            " RETURNING id",
            (
                Jsonb(summary),
                total,
                limit,
                explanation,
                token_id,
                asker,
                request_hash,
                version,
            ),
        ).fetchone()
        if not pending:
            pending = conn.execute(
                "INSERT INTO tl.approvals(token_id,asker,policy_version,request_hash,estimate,cost,limit_cost,explanation)"
                " VALUES ((SELECT id FROM tl.tokens WHERE id=%s),%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    token_id,
                    asker,
                    version,
                    request_hash,
                    Jsonb(summary),
                    total,
                    limit,
                    explanation,
                ),
            ).fetchone()
    raise ApprovalRequired(summary, limit, str(pending["id"]), explanation)


COLUMNS = "id,token_id,asker,policy_version,request_hash,estimate,cost,limit_cost,explanation,status,approved_by,created_at,decided_at"


def list_approvals(conn, *, token_id=None, asker=None, own=True, status=None):
    """Approvals, newest first. ``own`` restricts to one token and asker."""
    where, values = [], []
    if own:
        where.append(
            "token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s"
        )
        values += [token_id, asker]
    if status is not None:
        where.append("status=%s")
        values.append(status)
    return conn.execute(
        f"SELECT {COLUMNS} FROM tl.approvals"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY created_at DESC,id LIMIT 500",
        values,
    ).fetchall()


def decide(
    conn, approval_id, status, *, by, token_id=None, asker=None, own=False, limit=None
):
    """Approve or reject a pending approval; returns the row.

    ``own`` (an agent deciding) restricts to that token's and asker's own
    approvals, and ``limit`` is the highest cost it may approve. Raises
    LookupError when there is no such approval, PermissionError when the cost
    is above the limit and ValueError when it is no longer pending.
    """
    if status not in ("approved", "rejected"):
        raise ValueError("status must be approved or rejected")
    try:
        approval_id = str(uuid.UUID(str(approval_id)))
    except ValueError:
        raise LookupError("no such approval") from None
    row = conn.execute(
        f"SELECT {COLUMNS} FROM tl.approvals WHERE id=%s"
        + (
            " AND token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s"
            if own
            else ""
        )
        + " FOR UPDATE",
        (approval_id, token_id, asker) if own else (approval_id,),
    ).fetchone()
    if not row:
        raise LookupError("no such approval")
    if status == "approved" and limit is not None and float(row["cost"]) > float(limit):
        raise PermissionError("approval cost is above this token's approve_limit")
    if row["status"] != "pending":
        raise ValueError(f"approval is already {row['status']}")
    return conn.execute(
        f"UPDATE tl.approvals SET status=%s,approved_by=%s,decided_at=now() WHERE id=%s RETURNING {COLUMNS}",
        (status, by, approval_id),
    ).fetchone()
