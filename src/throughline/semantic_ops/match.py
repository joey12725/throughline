"""Semantic operator match: api.match(...) in SQL and op 'match' in plans.

    SELECT * FROM api.match(condition => 'mentions a prototype',
        input_sql => $$SELECT id FROM api.search_filtered('prototype', 200)$$,
        max_rows => 200) WHERE meta->>'verdict' = 'match'

Each member is judged match, nonmatch or uncertain (retrieval.match); a
failed evaluation is NULL with meta.error. The estimate covers only members
without a cached verdict.

Cost gate (CST-10). The SQL runtime calls estimate() and run() with the same
context and passes every estimate through cost.authorize for the whole
statement before anything runs; estimate() marks that context, and run() does
not authorize again (a second gate would be bound to another request and
refuse an approved statement). Any other caller, for example a plan node whose
input is produced by an earlier node and so was never estimated, gets a run()
that passes its own estimate through cost.authorize before any model call. A
caller that has authorized the estimate itself with another context sets
ctx.extras['authorized'] = True.
"""

from ..retrieval import match as library
from . import SemanticOp

PARAMS = {
    "condition": "text",
    "input": "handle",
    "max_rows": "int",
    "grain": "text",
}
# ctx.extras key set by estimate(): this context's estimate went to the
# caller's cost gate together with the rest of the request.
ESTIMATED = "match_estimated"


def _key(args):
    return (
        args.get("input"),
        args.get("condition"),
        args.get("max_rows"),
        args.get("grain") or "record",
    )


def _estimate(ctx, args):
    estimates = library.estimate(
        ctx.scope,
        handle=args.get("input"),
        condition=args.get("condition"),
        max_rows=args.get("max_rows"),
        grain=args.get("grain") or "record",
    )["estimates"]
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict):
        extras.setdefault(ESTIMATED, set()).add(_key(args))
    return estimates


def _authorized(ctx, args):
    extras = getattr(ctx, "extras", None)
    if not isinstance(extras, dict):
        return False
    return extras.get("authorized") is True or _key(args) in extras.get(ESTIMATED, ())


def _run(ctx, args):
    return library.match(
        ctx.scope,
        ctx.token_row,
        handle=args.get("input"),
        condition=args.get("condition"),
        max_rows=args.get("max_rows"),
        grain=args.get("grain") or "record",
        tracker=ctx.tracker,
        execution=ctx.execution,
        trace=ctx.trace,
        authorize=not _authorized(ctx, args),
    )


OP = SemanticOp(
    name="match",
    params=PARAMS,
    required=("condition", "input", "max_rows"),
    input_params=("input",),
    exact=False,
    cost_class="model",
    version=library.OPERATOR_VERSION,
    estimate=_estimate,
    run=_run,
    description="Judge every member of a set: match, nonmatch, uncertain; NULL when the evaluation failed",
)
