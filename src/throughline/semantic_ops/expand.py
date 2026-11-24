"""Semantic operator expand: api.expand(...) in SQL and op 'expand' in plans.

    SELECT * FROM api.expand(input => 'rs_...', mode => 'context', before => 2, after => 2)

The input set is grown by one mechanical relation (retrieval.expand): context,
thread, sources, links, time, place, or focus (approximate). Added members
have role context and depends_on their anchor; every added message passes
api.scope_filter. expand never calls a model, so its estimate is empty.
"""

from ..retrieval import expand as library
from ..retrieval.types import HardScope
from . import SemanticOp

PARAMS = {
    "input": "handle",
    "mode": "text",
    "before": "int",
    "after": "int",
    "link_type": "text",
    "depth": "int",
    "as_of": "timestamptz",
    "known_by": "timestamptz",
    "window_s": "int",
    "meters": "float",
    "focus": "text",
    "k": "int",
    "keep_input": "bool",
    "role": "text",
    "within": "json",
}
DEFAULTS = dict(
    before=3,
    after=3,
    depth=1,
    window_s=0,
    meters=100,
    k=20,
    keep_input=True,
    role="context",
)


def arguments(args):
    """Keyword arguments of retrieval.expand for operator arguments."""
    values = {k: v for k, v in args.items() if v is not None}
    for name, default in DEFAULTS.items():
        values.setdefault(name, default)
    within = values.pop("within", None)
    if within is not None:
        if not isinstance(within, dict):
            raise ValueError("within of api.expand must be a JSON object")
        within = HardScope.model_validate(within)
    handle = values.pop("input", None)
    return dict(values, handle=handle, within=within)


def _estimate(ctx, args):
    mode = args.get("mode")
    if mode not in library.MODES:
        raise ValueError("mode of api.expand must be " + ", ".join(library.MODES))
    return []


def _run(ctx, args):
    kwargs = arguments(args)
    return library.expand(
        ctx.scope, tracker=ctx.tracker, execution=ctx.execution, **kwargs
    )


OP = SemanticOp(
    name="expand",
    params=PARAMS,
    required=("input", "mode"),
    input_params=("input",),
    exact=True,
    cost_class="free",
    version="1",
    estimate=_estimate,
    run=_run,
    description="Expand a result set by context, thread, sources, links, time, place or focus",
)
