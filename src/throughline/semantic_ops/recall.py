"""Semantic operator recall: api.recall(...) in SQL and op 'recall' in plans.

    SELECT r.record_id, r.score, m.channel
    FROM api.recall(query => 'orchard', k => 10, channels => ARRAY['synthetic.chat']) r
    JOIN api.messages m ON m.id = r.message_id

The flat arguments map onto retrieval.RecallRequest (scope fields go into the
hard scope); options takes any further RecallRequest fields as JSON and is
merged under the flat arguments. The request is always saved as a result set
(save on), and hints.escalate is stripped: escalation to the librarian is a
route decision and never happens inside a statement or a plan. recall never
calls a semantic model or a controller, so its estimate is empty.
"""

from ..retrieval import recall as recall_library
from ..retrieval.types import HardScope, RecallRequest
from . import SemanticOp

PARAMS = {
    "query": "text",
    "k": "int",
    "channels": "text[]",
    "kinds": "text[]",
    "person": "int",
    "persons": "int[]",
    "after": "timestamptz",
    "before": "timestamptz",
    "time_basis": "text",
    "as_of": "timestamptz",
    "known_by": "timestamptz",
    "raw_only": "bool",
    "expand": "bool",
    "strategy": "text",
    "ranker": "text",
    "tokens": "int",
    "deadline_ms": "int",
    "group_by": "text",
    "exhaustive": "bool",
    "include_ignored": "bool",
    "options": "json",
}
SCOPE_FIELDS = (
    "channels",
    "kinds",
    "after",
    "before",
    "time_basis",
    "as_of",
    "known_by",
)
TOP_FIELDS = (
    "query",
    "k",
    "raw_only",
    "expand",
    "strategy",
    "ranker",
    "group_by",
    "exhaustive",
)


def _merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def request_of(args, remaining_ms=None):
    """RecallRequest for flat operator arguments (save on, no escalation)."""
    options = args.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options of api.recall must be a JSON object")
    body = {}
    for name in TOP_FIELDS:
        if args.get(name) is not None:
            body[name] = args[name]
    scope = {}
    for name in SCOPE_FIELDS:
        if args.get(name) is not None:
            value = args[name]
            scope[name] = value.isoformat() if hasattr(value, "isoformat") else value
    persons = list(args.get("persons") or [])
    if args.get("person") is not None:
        persons.append(args["person"])
    if persons:
        scope["persons"] = persons
    if args.get("include_ignored") is not None:
        # The overlay's ignore flag (annotations) is a hard-scope field when
        # present; before that nothing is excluded, so there is nothing to add.
        if "include_ignored" in HardScope.model_fields:
            scope["include_ignored"] = args["include_ignored"]
    if scope:
        body["scope"] = scope
    budgets = {}
    if args.get("tokens") is not None:
        budgets["tokens"] = args["tokens"]
    if args.get("deadline_ms") is not None:
        budgets["deadline_ms"] = args["deadline_ms"]
    if remaining_ms is not None:
        budgets["deadline_ms"] = int(
            min(budgets.get("deadline_ms", remaining_ms), max(1, remaining_ms))
        )
    if budgets:
        body["budgets"] = budgets
    merged = _merge(options, body)
    hints = dict(merged.get("hints") or {})
    hints.pop("escalate", None)
    merged["hints"] = hints
    merged["save"] = True
    return RecallRequest.model_validate(merged)


def _estimate(ctx, args):
    # recall never calls a semantic model or a controller (QRY-40).
    request_of(args)
    return []


def _run(ctx, args):
    remaining = ctx.tracker.remaining_ms() if ctx.tracker is not None else None
    request = request_of(args, remaining)
    response = recall_library(ctx.scope, request, token_row=ctx.token_row, trace=None)
    execution = response.execution or {}
    if ctx.trace is not None:
        ctx.trace.event(
            "semantic.recall",
            None,
            "ok" if response.result_set else "failed",
            rows=len(response.records),
            detail=dict(
                status=execution.get("status"),
                completeness=execution.get("completeness"),
                result_set=response.result_set,
            ),
        )
    if not response.result_set:
        raise RuntimeError("recall result set was not saved")
    if ctx.execution is not None:
        for item in execution.get("incomplete") or []:
            ctx.execution.degrade(
                "recall:" + str(item.get("stage")), item.get("reason") or "incomplete"
            )
        for warning in execution.get("warnings") or []:
            ctx.execution.warn(warning)
    ctx.extras["recall_execution"] = dict(
        status=execution.get("status"),
        completeness=execution.get("completeness"),
        counts=execution.get("counts"),
    )
    return response.result_set


OP = SemanticOp(
    name="recall",
    params=PARAMS,
    required=("query",),
    input_params=(),
    exact=False,
    cost_class="free",
    version="1",
    estimate=_estimate,
    run=_run,
    description="Recall records of every kind under a hard scope; saved as a result set",
)
