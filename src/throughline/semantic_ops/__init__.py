"""Semantic operators of the SQL runtime and of plans, discovered by module scan.

A module in this package that exports OP (an instance of SemanticOp) adds an
operator. Its name becomes a semantic function api.<name> that SQL may use as
the single function of a FROM item (throughline.semantic) and an op of plans
(throughline.plans). A semantic function is never a Postgres function and is
never on the query allow-list: before validation each call is replaced by
api.result_set('<handle>') of the set the operator produced.

Interface for operator modules (recall here; expand, match, librarian in
their own modules):

  name           the function name (lower case)
  params         {argument name: type}, in positional order. Types: text, int,
                 float, bool, json, timestamptz, text[], int[], handle
  required       argument names that must be given
  input_params   arguments that take a result set. In SQL each may be given
                 as input => 'rs_<32 hex>' or as input_sql => '<SELECT>' with
                 max_rows => n (1..10000): the runtime runs the statement and
                 passes the handle of a set of its rows. run() always receives
                 a handle.
  exact          whether the output is an exact answer
  cost_class     free | embedding | model
  version        operator version
  top_level_only only allowed alone in the top-level FROM (librarian)
  degradable     a failure leaves an empty truncated set instead of failing
                 the statement
  estimate(ctx, args) -> [providers.base.Estimate]  before anything runs
  run(ctx, args)      -> handle of a new result set owned by ctx.scope

OpContext carries scope, token_row, tracker (the request's BudgetTracker),
execution (the request's Execution), trace (the request's Trace) and
in_investigation (true inside a librarian investigation). An operator spends
only through providers.call(..., tracker=ctx.tracker), reads only under
ctx.scope, and returns a handle; it never returns rows.
"""

import importlib
import json
import logging
import pkgutil
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("throughline.semantic_ops")

TYPES = (
    "text",
    "int",
    "float",
    "bool",
    "json",
    "timestamptz",
    "text[]",
    "int[]",
    "handle",
)
COST_CLASSES = ("free", "embedding", "model")
HANDLE = re.compile(r"^rs_[0-9a-f]{32}$")
NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

_extra = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class SemanticOp:
    name: str
    params: dict
    required: tuple = ()
    input_params: tuple = ()
    exact: bool = False
    cost_class: str = "free"
    version: str = "1"
    top_level_only: bool = False
    degradable: bool = False
    estimate: Callable | None = None
    run: Callable | None = None
    description: str = ""

    def __post_init__(self):
        if not NAME.fullmatch(self.name or ""):
            raise ValueError("semantic operator name must be a lower-case name")
        if self.cost_class not in COST_CLASSES:
            raise ValueError("cost_class must be free, embedding or model")
        params = dict(self.params)
        for name, kind in params.items():
            if not NAME.fullmatch(name) or kind not in TYPES:
                raise ValueError(f"{self.name}: invalid parameter {name} {kind}")
        for name in tuple(self.required) + tuple(self.input_params):
            if name not in params:
                raise ValueError(f"{self.name}: {name} is not a parameter")
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "required", tuple(self.required))
        object.__setattr__(self, "input_params", tuple(self.input_params))
        if self.run is None:
            raise ValueError(f"{self.name}: run is required")

    def estimates(self, ctx, args):
        if self.estimate is None:
            return []
        return list(self.estimate(ctx, args) or [])

    def describe(self):
        return dict(
            name=self.name,
            params=dict(self.params),
            required=list(self.required),
            input_params=list(self.input_params),
            exact=self.exact,
            cost_class=self.cost_class,
            version=self.version,
            top_level_only=self.top_level_only,
            degradable=self.degradable,
        )


@dataclass
class OpContext:
    scope: Any
    token_row: dict | None
    tracker: Any
    execution: Any
    trace: Any = None
    in_investigation: bool = False
    extras: dict = field(default_factory=dict)


def register(op):
    """Add an operator that is not a module of this package (tests, plugins)."""
    if not isinstance(op, SemanticOp):
        raise TypeError("a SemanticOp is required")
    with _lock:
        _extra[op.name] = op
    return op


def unregister(name):
    with _lock:
        return _extra.pop(name, None)


def discover():
    """{name: SemanticOp} of every module exporting OP, plus registered ones.

    A module that fails to import is skipped with a warning: a broken
    operator must not take the SQL surface down with it.
    """
    found = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # pragma: no cover - depends on siblings
            log.warning("semantic operator %s not loaded: %s", info.name, exc)
            continue
        op = getattr(module, "OP", None)
        if isinstance(op, SemanticOp):
            found[op.name] = op
    with _lock:
        found.update(_extra)
    return dict(sorted(found.items()))


def names():
    return set(discover())


# --- argument types ----------------------------------------------------------


def _time(value, name):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def coerce(op, name, value):
    """The value of one argument in the parameter's type (ValueError if not)."""
    kind = op.params[name]
    if value is None:
        return None
    try:
        if kind == "text":
            if isinstance(value, (dict, list)):
                raise ValueError
            return str(value)
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            return int(value)
        if kind == "float":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "t", "on", "1"):
                return True
            if isinstance(value, str) and value.lower() in ("false", "f", "off", "0"):
                return False
            raise ValueError
        if kind == "json":
            return json.loads(value) if isinstance(value, str) else value
        if kind == "timestamptz":
            return _time(value, name)
        if kind in ("text[]", "int[]"):
            if not isinstance(value, (list, tuple)):
                value = [value]
            inner = "text" if kind == "text[]" else "int"
            probe = SemanticOp(
                name="probe", params={"v": inner}, run=lambda ctx, args: None
            )
            return [coerce(probe, "v", v) for v in value]
        if kind == "handle":
            if not isinstance(value, str) or not HANDLE.fullmatch(value):
                raise ValueError
            return value
    except (TypeError, ValueError):
        raise ValueError(
            f"argument {name} of api.{op.name} must be of type {kind}"
        ) from None
    raise ValueError(f"argument {name} of api.{op.name} has an unknown type")


def bind(op, positional, named):
    """{parameter: value} from positional and named arguments, checked.

    Unknown or repeated parameters and missing required ones raise ValueError
    naming the parameter. Input parameters are left unresolved here.
    """
    order = list(op.params)
    args = {}
    if len(positional) > len(order):
        raise ValueError(
            f"semantic function api.{op.name} is not supported here: too many arguments"
        )
    for name, value in zip(order, positional):
        args[name] = value
    for name, value in named.items():
        if name not in op.params:
            raise ValueError(
                f"semantic function api.{op.name} is not supported here: unknown parameter {name}"
            )
        if name in args:
            raise ValueError(
                f"semantic function api.{op.name} is not supported here: parameter {name} given twice"
            )
        args[name] = value
    for name in op.required:
        if args.get(name) is None and name not in op.input_params:
            raise ValueError(
                f"semantic function api.{op.name} is not supported here: missing parameter {name}"
            )
    return {
        name: value if name in op.input_params else coerce(op, name, value)
        for name, value in args.items()
    }
