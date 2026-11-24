"""Recall stages, discovered by module scan and run in order after fusion.

A module in this package that exports STAGE (an instance of Stage) adds a
stage. Reserved orders: page_sources 5, spans 10, rerank 20, dedup 30,
select 40, group 50.

  name, order
  exact           whether the stage keeps an exact result exact
  enabled(ctx)    -> bool
  run(ctx, candidates) -> candidates (the list to continue with)

A failing stage is skipped and reported; the candidates it received continue.
Rule: a statement that creates a role 'hit' candidate applies api.scope_filter
with the full hard scope; dependency and context rows are filtered with kinds,
as_of, known_by and include_outside only (scope_sql.args(resolved, reduced=True)).
"""

import importlib
import pkgutil
import threading

ORDERS = dict(page_sources=5, spans=10, rerank=20, dedup=30, select=40, group=50)

_extra = {}
_lock = threading.Lock()


class Stage:
    name = ""
    order = 100
    exact = True

    def enabled(self, ctx):
        return True

    def run(self, ctx, candidates):
        return candidates


def register(stage):
    """Add a stage that is not a module of this package (tests, plugins)."""
    with _lock:
        _extra[stage.name] = stage
    return stage


def unregister(name):
    with _lock:
        return _extra.pop(name, None)


def discover():
    """Stages in (order, name) order."""
    found = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        stage = getattr(module, "STAGE", None)
        if isinstance(stage, Stage) and stage.name:
            found[stage.name] = stage
    with _lock:
        found.update(_extra)
    return sorted(found.values(), key=lambda s: (s.order, s.name))
