"""HTTP route modules, discovered by create_app.

A module in this package that exports `router` (a fastapi.APIRouter with full
paths such as /v1/traces/{trace_id}) is included automatically, in module-name
order. Every included route depends on authentication whether or not its
handler declares a Token, so it is audited and listed with security in the
OpenAPI document. Import shared plumbing from throughline.api.deps.
"""

import importlib
import pkgutil


def routers():
    """(module name, router) of every route module, sorted by name."""
    found = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            found.append((info.name, router))
    return found
