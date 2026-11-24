"""Semantic operator librarian: api.librarian(...) in SQL (QRY-39).

    SELECT l.record_id, l.kind
    FROM api.librarian(goal => 'orchard', deadline_ms => 6000, tokens => 6000) l

Only alone in the top-level FROM (top_level_only), never a plan op
(plans.REFUSED_OPS) and refused inside an investigation (ctx.in_investigation):
a librarian never starts a librarian. The rows are the delivered result set;
the packet-level fields (orientation notes, coverage, unresolved needs,
status, continuation_ref) are in execution.packet. request takes any further
field of the request envelope as JSON; the flat arguments win over it.
"""

from ..providers.base import ProviderUnavailable
from . import SemanticOp

PARAMS = {
    "goal": "text",
    "deadline_ms": "int",
    "tokens": "int",
    "request": "json",
}


def _request(args, ctx=None):
    from ..librarian.request import LibrarianRequest

    body = args.get("request") or {}
    if not isinstance(body, dict):
        raise ValueError("request of api.librarian must be a JSON object")
    body = dict(body)
    budgets = dict(body.get("budgets") or {})
    if args.get("deadline_ms") is not None:
        budgets["deadline_ms"] = args["deadline_ms"]
    if args.get("tokens") is not None:
        budgets["tokens"] = args["tokens"]
    tracker = getattr(ctx, "tracker", None)
    if tracker is not None:
        # The statement's budget is the ceiling of the investigation's.
        remaining = tracker.remaining_ms()
        if remaining is not None:
            budgets["deadline_ms"] = int(
                min(budgets.get("deadline_ms", remaining), max(1, remaining))
            )
        snapshot = tracker.snapshot()
        limit = snapshot["limits"].get("model_cost")
        if limit is not None:
            left = max(0.0, limit - snapshot["spent"]["model_cost"])
            budgets["model_cost"] = min(budgets.get("model_cost", left), left)
    if budgets:
        body["budgets"] = budgets
    if args.get("goal") is not None:
        body["goal"] = args["goal"]
    return LibrarianRequest.model_validate(body)


def _estimate(ctx, args):
    if getattr(ctx, "in_investigation", False):
        raise ValueError("api.librarian cannot run inside an investigation")
    from .. import providers
    from ..librarian.brief import TEXT

    request = _request(args)
    try:
        controller = providers.get("controller")
    except ProviderUnavailable:
        return []
    turns = request.budgets.controller_turns
    estimate = controller.estimate(
        TEXT, {"goal": request.goal}, turns=max(1, 2 if turns is None else turns)
    )
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict):
        extras["librarian_estimated"] = True
    return [estimate]


def _run(ctx, args):
    if getattr(ctx, "in_investigation", False) or getattr(
        ctx.scope, "investigation_id", None
    ):
        raise ValueError("api.librarian cannot run inside an investigation")
    from .. import resultsets
    from ..librarian import investigate

    extras = ctx.extras if isinstance(getattr(ctx, "extras", None), dict) else {}
    authorized = bool(extras.get("authorized") or extras.get("librarian_estimated"))
    packet = investigate(
        ctx.scope, ctx.token_row, _request(args, ctx), authorized=authorized
    )
    if ctx.tracker is not None:
        spent = (packet.get("cost_and_latency") or {}).get("model_cost") or 0
        ctx.tracker.reserve(float(spent), 0)
    if ctx.execution is not None:
        ctx.execution.set("packet", {k: v for k, v in packet.items() if k != "records"})
        if packet.get("execution_status") != "complete":
            ctx.execution.degrade(
                "librarian", packet.get("incompleteness_reason") or "degraded"
            )
    if ctx.trace is not None:
        ctx.trace.event(
            "semantic.librarian",
            None,
            packet.get("execution_status"),
            rows=len(packet.get("records") or []),
            detail=dict(
                investigation_id=packet.get("investigation_id"),
                result_set=packet.get("result_set_ref"),
            ),
        )
    handle = packet.get("result_set_ref")
    if handle:
        return handle
    return resultsets.create(
        ctx.scope,
        [],
        origin=dict(op="librarian", failed=True),
        completeness="truncated",
    )


OP = SemanticOp(
    name="librarian",
    params=PARAMS,
    required=("goal",),
    input_params=(),
    exact=False,
    cost_class="model",
    version="1",
    top_level_only=True,
    estimate=_estimate,
    run=_run,
    description="Optional bounded investigation; rows are the delivered result set, execution.packet the packet",
)
