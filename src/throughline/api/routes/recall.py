"""POST /v1/recall: the shared retrieval library over HTTP (QRY-13, INT-10).

Every statement runs on the reader path under the token's scope (the asker
gate applies in the database), so a restricted asker may call it. The audit
row records counts and the trace id, never the request body.

Escalation (QRY-40, CST-10) happens here and only here, after recall()
returns, never by default. All of these must hold: the token has
allow_escalation; the request carries hints.escalate = {librarian: true,
budgets: {...}} with at least one budget; and the result has fewer than k
records or a must_term (hints.must_terms or scope.must_terms) that no record
contains. The response is then the librarian's packet with escalated: true
and the investigation id. Plans, SQL and the library function never escalate.
"""

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder

from ...retrieval import recall as recall_library
from ...retrieval.types import RecallRequest
from ..deps import Token, audit, require, scope_of

router = APIRouter()

BUDGET_KEYS = ("deadline_ms", "tokens", "model_cost", "model_calls", "controller_turns")


def escalation(data, token):
    """The escalate hint's budgets when escalation is authorized, else None."""
    hint = (data.hints or {}).get("escalate")
    if not isinstance(hint, dict) or hint.get("librarian") is not True:
        return None
    if token.get("allow_escalation") is not True:
        return None
    budgets = hint.get("budgets")
    if not isinstance(budgets, dict):
        return None
    budgets = {k: v for k, v in budgets.items() if k in BUDGET_KEYS and v is not None}
    return budgets or None


def uncovered_terms(data, records):
    terms = list((data.hints or {}).get("must_terms") or [])
    terms += list(data.scope.must_terms or [])
    texts = [str(r.get("content") or "").lower() for r in records]
    return [
        str(t)
        for t in terms
        if str(t).strip() and not any(str(t).lower() in text for text in texts)
    ]


def _escalate(data, token, budgets, response):
    from ...librarian import investigate

    request = dict(
        goal=data.query or "recall",
        scope=data.scope.model_dump(exclude_unset=True),
        budgets=budgets,
        exclude_seen=list(data.exclude_seen or []),
        starting_sets=list(data.starting_sets or [])
        + ([response.result_set] if response.result_set else []),
    )
    return investigate(scope_of(token), token, request)


@router.post("/v1/recall")
def recall(data: RecallRequest, token: Token):
    """Recall records of every kind under a hard scope; see api.describe('recall')."""
    require(token, "owner", "agent")
    if data.view == "auto" and data.excerpt_chars is None:
        data = data.model_copy(update=dict(excerpt_chars=4000))
    response = recall_library(scope_of(token), data, token_row=token)
    execution = response.execution
    counts = execution.get("counts") or {}
    audit(
        token,
        len(response.records),
        operation="recall",
        legs=sorted((counts.get("per_leg") or {}).keys()),
        retrieved=counts.get("retrieved"),
        status=execution.get("status"),
        **({"trace_id": execution["trace_id"]} if execution.get("trace_id") else {}),
        **({"result_set": response.result_set} if response.result_set else {}),
    )
    budgets = escalation(data, token)
    if budgets is not None and (
        len(response.records) < data.k or uncovered_terms(data, response.records)
    ):
        try:
            packet = _escalate(data, token, budgets, response)
        except (ValueError, LookupError, PermissionError) as exc:
            result = response.to_dict()
            result["extras"] = dict(
                result.get("extras") or {},
                escalation=dict(attempted=True, error=str(exc)[:200]),
            )
            return jsonable_encoder(result)
        audit(
            token,
            len(packet["records"]),
            escalated=True,
            investigation_id=packet["investigation_id"],
        )
        packet = dict(packet, escalated=True, recall_execution=execution)
        return jsonable_encoder(packet)
    result = jsonable_encoder(response.to_dict())
    if data.view == "auto":
        from ...agent_view import shrink_recall

        result, _ = shrink_recall(result, data.query, data.max_chars)
    return result
