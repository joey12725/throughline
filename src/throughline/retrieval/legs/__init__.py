"""Retrieval legs, discovered by module scan.

A module in this package that exports LEG (an instance of Leg) adds a leg.
A leg generates candidates for one round of recall:

  name            unique leg name (what RecallRequest.legs and planners use)
  exact           whether its candidates are an exact answer (keyword match,
                  enumeration) or a similarity ranking
  cost_class      free | embedding | model
  kinds           record kinds it can return
  honours_as_of   bool, or callable(ctx) -> bool: whether as_of/known_by are
                  applied at candidate generation
  auto            whether the default planner includes it in strategy 'auto'
  available(ctx)  -> (bool, reason); an unavailable leg is skipped and the
                  reason disclosed in execution
  run(ctx)        -> [Candidate] in rank order

Rule for every leg: a statement that creates a candidate with role 'hit'
applies api.scope_filter (or a 0038 leg function, which applies it) with the
full hard scope before any limit. Dependency and context rows are filtered
with the reduced scope (kinds, as_of, known_by, include_outside) only.
"""

import importlib
import pkgutil
import threading

from ... import operators

_extra = {}
_lock = threading.Lock()


class Leg:
    name = ""
    exact = True
    cost_class = "free"
    kinds = ()
    honours_as_of = True
    auto = False
    implements = None
    implementation = None

    def available(self, ctx):
        return True, None

    def run(self, ctx):
        return []

    def honours(self, ctx):
        value = self.honours_as_of
        return bool(value(ctx)) if callable(value) else bool(value)

    def spec(self):
        return operators.OperatorSpec(
            name="leg." + self.name,
            implements=self.implements or self.name + "_candidates",
            implementation=self.implementation or self.name,
            kind="leg",
            inputs=("scope", "query"),
            outputs=("candidates",),
            exact=self.exact,
            cost_class=self.cost_class,
        )


def register(leg):
    """Add a leg that is not a module of this package (tests, plugins)."""
    with _lock:
        _extra[leg.name] = leg
    return leg


def unregister(name):
    with _lock:
        return _extra.pop(name, None)


def discover():
    """{name: Leg} of every module exporting LEG, plus registered legs."""
    found = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        leg = getattr(module, "LEG", None)
        if isinstance(leg, Leg) and leg.name:
            found[leg.name] = leg
    with _lock:
        found.update(_extra)
    for leg in found.values():
        try:
            operators.register(leg.spec(), replace=True)
        except Exception:  # A bad spec never stops recall.
            pass
    return found


def message_kinds_ok(ctx):
    """(bool, reason) for legs that return message records."""
    if ctx.resolved.kinds == []:
        return False, "kinds_excluded"
    channels = ctx.resolved.scope.channels
    if channels:
        from ...db import connect

        with connect("app") as conn:
            row = conn.execute(
                "SELECT bool_or(record_kind='page_revision') AS has_pages,"
                " bool_or(record_kind<>'page_revision') AS has_messages"
                " FROM tl.channels WHERE name=ANY(%s)",
                (list(channels),),
            ).fetchone()
        if row and row["has_pages"] and not row["has_messages"]:
            return False, "kinds_excluded"
    return True, None


def query_ok(ctx):
    if not (ctx.request.query or "").strip():
        return False, "no_query"
    return True, None
